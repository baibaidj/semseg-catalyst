import os
import time
import logging
import argparse

import cv2
import numpy as np
import torch
import torch.backends.cudnn as cudnn
import torch.nn.functional as F
import torch.nn.parallel
import torch.utils.data
from pathlib import Path

from util import dataset, transform, config
from util.util import AverageMeter, intersectionAndUnion, check_makedirs, colorize
# from tool.visual import visual_gt_pred_1slice, Path
# from util.dataset import convert_label, pil_load

cv2.ocl.setUseOpenCL(False)


def get_parser():
    parser = argparse.ArgumentParser(description='PyTorch Semantic Segmentation')
    parser.add_argument('--config', type=str, default='config/ade20k/ade20k_pspnet50.yaml', help='config file')
    parser.add_argument('opts', help='see config/ade20k/ade20k_pspnet50.yaml for all options', default=None, nargs=argparse.REMAINDER)
    args = parser.parse_args()
    assert args.config is not None
    cfg = config.load_cfg_from_cfg_file(args.config)
    if args.opts is not None:
        cfg = config.merge_cfg_from_list(cfg, args.opts)
    return cfg


def get_logger():
    logger_name = "main-logger"
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    fmt = "[%(asctime)s %(levelname)s %(filename)s line %(lineno)d %(process)d] %(message)s"
    handler.setFormatter(logging.Formatter(fmt))
    logger.addHandler(handler)
    return logger


def check(args):
    assert args.classes > 1
    assert args.zoom_factor in [1, 2, 4, 8]
    assert args.split in ['train', 'val', 'test']
    if args.arch == 'psp' or 'psp' in args.arch:
        assert (args.train_h - 1) % 8 == 0 and (args.train_w - 1) % 8 == 0
    elif args.arch == 'psa':
        if args.compact:
            args.mask_h = (args.train_h - 1) // (8 * args.shrink_factor) + 1
            args.mask_w = (args.train_w - 1) // (8 * args.shrink_factor) + 1
        else:
            assert (args.mask_h is None and args.mask_w is None) or (args.mask_h is not None and args.mask_w is not None)
            if args.mask_h is None and args.mask_w is None:
                args.mask_h = 2 * ((args.train_h - 1) // (8 * args.shrink_factor) + 1) - 1
                args.mask_w = 2 * ((args.train_w - 1) // (8 * args.shrink_factor) + 1) - 1
            else:
                assert (args.mask_h % 2 == 1) and (args.mask_h >= 3) and (
                        args.mask_h <= 2 * ((args.train_h - 1) // (8 * args.shrink_factor) + 1) - 1)
                assert (args.mask_w % 2 == 1) and (args.mask_w >= 3) and (
                        args.mask_w <= 2 * ((args.train_h - 1) // (8 * args.shrink_factor) + 1) - 1)
    else:
        raise Exception('architecture not supported yet'.format(args.arch))


def main():
    global args, logger
    args = get_parser()
    check(args)
    logger = get_logger()
    os.environ["CUDA_VISIBLE_DEVICES"] = ','.join(str(x) for x in args.test_gpu)
    logger.info(args)
    logger.info("=> creating model ...")
    logger.info("Classes: {}".format(args.classes))

    value_scale = 255
    mean = [0.485, 0.456, 0.406]
    mean = [item * value_scale for item in mean]
    std = [0.229, 0.224, 0.225]
    std = [item * value_scale for item in std]

    gray_folder = os.path.join(args.save_folder, 'gray')
    color_folder = os.path.join(args.save_folder, 'color')

    test_transform = transform.Compose([transform.ToTensor()])
    test_data = dataset.SemData(split=args.split, 
                                data_root=args.data_root, 
                                data_list=args.test_list, 
                                transform=test_transform
                                )
    index_start = args.index_start
    if args.index_step == 0:
        index_end = len(test_data.data_list)
    else:
        index_end = min(index_start + args.index_step, len(test_data.data_list))
    test_data.data_list = test_data.data_list[index_start:index_end]
    test_loader = torch.utils.data.DataLoader(test_data, batch_size=1, shuffle=False, num_workers=args.workers, pin_memory=True)
    colors = np.loadtxt(args.colors_path).astype('uint8')
    names = [line.rstrip('\n') for line in open(args.names_path)]

    if not args.has_prediction:
        print('arch: ', args.arch)
        if args.arch == 'psp':
            from model.pspnet import PSPNet
            model = PSPNet(layers=args.layers, classes=args.classes, zoom_factor=args.zoom_factor, pretrained=False)
        elif args.arch == 'psa':
            from model.psanet import PSANet
            model = PSANet(layers=args.layers, classes=args.classes, zoom_factor=args.zoom_factor, compact=args.compact,
                           shrink_factor=args.shrink_factor, mask_h=args.mask_h, mask_w=args.mask_w,
                           normalization_factor=args.normalization_factor, psa_softmax=args.psa_softmax, pretrained=False)
        elif 'of' in args.arch:
            from catalyst_cityscapes.model.mymodel import ofPSPNet
            print('ofPSPNet')
            model = ofPSPNet(encoder_name=str(args.layers), classes=args.classes, zoom_factor=args.zoom_factor, pretrained=False) 

        elif 'smp' in args.arch:
            from catalyst_cityscapes.model.mymodel import smpPSPNet
            print('smpPSPNet')
            model = smpPSPNet(encoder_name='resnet%d'%args.layers, classes=args.classes) 
        
        # logger.info(model)
        model = torch.nn.DataParallel(model).cuda()
        cudnn.benchmark = True
        if os.path.isfile(args.model_path):
            logger.info("=> loading checkpoint '{}'".format(args.model_path))
            if 'of' in args.arch or 'smp' in args.arch:
                from catalyst import utils
                checkpoint = utils.load_checkpoint(args.model_path)
                print('checkpoint keys', list(checkpoint))
                utils.unpack_checkpoint(checkpoint, model=model)
            else:
                checkpoint = torch.load(args.model_path)
                model.load_state_dict(checkpoint['state_dict'], strict=False)

            logger.info("=> loaded checkpoint '{}'".format(args.model_path))
        else:
            raise RuntimeError("=> no checkpoint found at '{}'".format(args.model_path))
        test(test_loader, test_data.data_list, model, args.classes, mean, std, args.base_size, 
            args.test_h, args.test_w, args.scales, gray_folder, color_folder, colors, is_med = args.get('is_med', False))
    if args.split != 'test':
        cal_acc(test_data.data_list, gray_folder, args.classes, names, 
                is_med = args.get('is_med', False), 
                label_mapping= args.get('label_mapping', None))


def net_process(model, image, mean, std=None, flip=True):

    # print('image: %s %s %s' %(image.shape, np.min(image), np.max(image)))
    # print('mean:%s, std:%s' %(mean, std))

    input = torch.from_numpy(image.transpose((2, 0, 1))).float()
    if std is None:
        for t, m in zip(input, mean):
            t.sub_(m)
    else:
        for t, m, s in zip(input, mean, std):
            t.sub_(m).div_(s)
    input = input.unsqueeze(0).cuda()
    if flip:
        input = torch.cat([input, input.flip(3)], 0)

    # print('input-tensor: %s %s %s' %(input.shape, input.min(), image.max()))

    with torch.no_grad():
        output = model(input)
        if type(output) is tuple:
            output = output[0]
        # print('output-tensor:', type(output), output.shape, output.min(), output.max())
    _, _, h_i, w_i = input.shape
    _, _, h_o, w_o = output.shape
    if (h_o != h_i) or (w_o != w_i):
        output = F.interpolate(output, (h_i, w_i), mode='bilinear', align_corners=True)
    output = F.softmax(output, dim=1)
    if flip:
        output = (output[0] + output[1].flip(2)) / 2
    else:
        output = output[0]
    output = output.data.cpu().numpy()
    output = output.transpose(1, 2, 0)
    return output


def scale_process(model, image, classes, crop_h, crop_w, h, w, mean, std=None, stride_rate=2/3):
    """
    """
    ori_h, ori_w, _ = image.shape # 设置的底版大小2048
    pad_h = max(crop_h - ori_h, 0) 
    pad_w = max(crop_w - ori_w, 0)
    pad_h_half = int(pad_h / 2)
    pad_w_half = int(pad_w / 2)
    if pad_h > 0 or pad_w > 0:
        image = cv2.copyMakeBorder(image, pad_h_half, pad_h - pad_h_half, pad_w_half, pad_w - pad_w_half, cv2.BORDER_CONSTANT, value=mean)
    new_h, new_w, _ = image.shape # 2048, 2048
    stride_h = int(np.ceil(crop_h*stride_rate)) # 476
    stride_w = int(np.ceil(crop_w*stride_rate)) # 
    grid_h = int(np.ceil(float(new_h-crop_h)/stride_h) + 1) # 2048-713 / 476
    grid_w = int(np.ceil(float(new_w-crop_w)/stride_w) + 1) 
    prediction_crop = np.zeros((new_h, new_w, classes), dtype=float)
    count_crop = np.zeros((new_h, new_w), dtype=float)
    print('grid',  grid_h, grid_w)
    for index_h in range(0, grid_h):
        for index_w in range(0, grid_w):
            s_h = index_h * stride_h
            e_h = min(s_h + crop_h, new_h)
            s_h = e_h - crop_h
            s_w = index_w * stride_w
            e_w = min(s_w + crop_w, new_w)
            s_w = e_w - crop_w
            image_crop = image[s_h:e_h, s_w:e_w].copy()
            count_crop[s_h:e_h, s_w:e_w] += 1
            prediction_crop[s_h:e_h, s_w:e_w, :] += net_process(model, image_crop, mean, std)
    prediction_crop /= np.expand_dims(count_crop, 2)
    prediction_crop = prediction_crop[pad_h_half:pad_h_half+ori_h, pad_w_half:pad_w_half+ori_w]
    prediction = cv2.resize(prediction_crop, (w, h), interpolation=cv2.INTER_LINEAR)
    return prediction


def scale_process_direct(model, image, classes, crop_h, crop_w, h, w, mean, std=None, stride_rate=2/3):
    """
    """
    # ori_h, ori_w, _ = image.shape # 设置的底版大小2048
    image = cv2.copyMakeBorder(image, 1, 0, 1, 0, cv2.BORDER_CONSTANT, value=mean)
    prediction_crop = net_process(model, image, mean, std)
    prediction = cv2.resize(prediction_crop, (w, h), interpolation=cv2.INTER_LINEAR)
    return prediction




def test(test_loader, data_list, model, classes, mean, std, base_size, crop_h, crop_w, 
        scales, gray_folder, color_folder, colors, is_multi_patch = True, is_med = False):
    """
    crop_h, crop_w = 713, 713
    
    """
    logger.info('>>>>>>>>>>>>>>>> Start Evaluation >>>>>>>>>>>>>>>>')
    data_time = AverageMeter()
    batch_time = AverageMeter()
    model.eval()
    end = time.time()
    for i, (input, gt) in enumerate(test_loader):
        data_time.update(time.time() - end)
        input = np.squeeze(input.numpy(), axis=0)
        image = np.transpose(input, (1, 2, 0))
        h, w, _ = image.shape #原始图像尺寸
        prediction = np.zeros((h, w, classes), dtype=float)
        for scale in scales:
            long_size = round(scale * base_size) # 推理中图像底板设置成2048
            new_h = long_size
            new_w = long_size
            if h > w:
                new_w = round(long_size/float(h)*w)
            else:
                new_h = round(long_size/float(w)*h)
            image_scale = cv2.resize(image, (new_w, new_h), interpolation=cv2.INTER_LINEAR) # 
            if is_multi_patch:
                print('inplace multi inference with patches')
                prediction += scale_process(model, image_scale, classes, crop_h, crop_w, h, w, mean, std)
            else:
                print('inplane one inference')
                #prediction += scale_process_direct(model, image_scale, classes, crop_h, crop_w, h, w, mean, std)
                prediction += scale_process(model, image_scale, classes, new_h+ 1, new_w+1, h, w, mean, std)                
        prediction /= len(scales)
        prediction = np.argmax(prediction, axis=2)
        batch_time.update(time.time() - end)
        end = time.time()
        if ((i + 1) % 10 == 0) or (i + 1 == len(test_loader)):
            logger.info('Test: [{}/{}] '
                        'Data {data_time.val:.3f} ({data_time.avg:.3f}) '
                        'Batch {batch_time.val:.3f} ({batch_time.avg:.3f}).'.format(i + 1, len(test_loader),
                                                                                    data_time=data_time,
                                                                                    batch_time=batch_time))
        
        _, target_path = data_list[i]
        image_name = get_image_name(target_path, is_med)
        # print(image_name)

        check_makedirs(gray_folder)
        gray = np.uint8(prediction)
        gray_path = os.path.join(gray_folder, image_name)
        check_makedirs(Path(gray_path).parent)
        cv2.imwrite(gray_path, gray)

        check_makedirs(color_folder)
        color = colorize(gray, colors)
        color_path = os.path.join(color_folder, image_name)
        color.save(color_path)


    logger.info('<<<<<<<<<<<<<<<<< End Evaluation <<<<<<<<<<<<<<<<<')


def get_image_name(image_path, is_med = False):
    if is_med:
        image_name = os.sep.join(image_path.split(os.sep)[-5:])
    else:
        image_name = image_path.split('/')[-1] #.split('.')[0] 
    # image_name = image_name.replace('/', '_')
    image_name = image_name.replace('label', 'pred')
    return image_name


def cal_acc(data_list, pred_folder, classes, names, is_med = False, label_mapping = None):
    intersection_meter = AverageMeter()
    union_meter = AverageMeter()
    target_meter = AverageMeter()

    for i, (image_path, target_path) in enumerate(data_list):
        image_name = get_image_name(target_path, is_med)
        pred_fp =  os.path.join(pred_folder, image_name)
        # print('pred : %s \n target : %s' %(pred_fp, target_path))
        pred = cv2.imread(pred_fp, cv2.IMREAD_GRAYSCALE)
        target = cv2.imread(target_path, cv2.IMREAD_GRAYSCALE) 
        if label_mapping is not None:
            target = convert_label(target, label_mapping)
        # print('pred: %s target: %s' %(np.unique(pred), np.unique(target)))

        intersection, union, target = intersectionAndUnion(pred, target, classes)
        intersection_meter.update(intersection)
        union_meter.update(union)
        target_meter.update(target)
        accuracy = sum(intersection_meter.val) / (sum(target_meter.val) + 1e-10)
        dice = sum(intersection_meter.val[1:])*2 /(
                sum(intersection_meter.val[1:]) + sum(union_meter.val[1:]) + 1e-10)
        logger.info('Evaluating {0}/{1} on image {2}, accuracy {3:.4f}, dice {4:.4f}.'.format(
                                i + 1, len(data_list), image_name, accuracy, dice))

    iou_class = intersection_meter.sum / (union_meter.sum + 1e-10)
    accuracy_class = intersection_meter.sum / (target_meter.sum + 1e-10)
    dice_class = intersection_meter.sum * 2  / (union_meter.sum + intersection_meter.sum + 1e-10)
    mDice = np.mean(dice_class[1:])
    mIoU = np.mean(iou_class)
    mAcc = np.mean(accuracy_class)
    allAcc = sum(intersection_meter.sum) / (sum(target_meter.sum) + 1e-10)

    logger.info('Eval result: mIoU/mAcc/allAcc/mDice {:.4f}/{:.4f}/{:.4f}/{:.4f}.'.format(mIoU, mAcc, allAcc, mDice))
    for i in range(classes):
        logger.info('Class_{} result: iou/accuracy/dice {:.4f}/{:.4f}/{:.4f}, name: {}.'.format(
                            i, iou_class[i], accuracy_class[i], dice_class[i],names[i]))


if __name__ == '__main__':
    main()
