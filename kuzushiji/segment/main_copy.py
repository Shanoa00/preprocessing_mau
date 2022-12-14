r"""PyTorch Detection Training.

To run in a multi-gpu environment, use the distributed launcher::

    python -m torch.distributed.launch --nproc_per_node=$NGPU --use_env \
        main.py ... --world-size $NGPU

"""
import argparse
#from cProfile import label
import datetime
from pathlib import Path
import time

import json_log_plots
import pandas as pd
import torch
import torch.utils.data
from torch import nn, tensor
from torch.optim.lr_scheduler import CosineAnnealingLR, MultiStepLR
import detection
from detection.rpn import AnchorGenerator
from detection.transform import GeneralizedRCNNTransform
from detection.faster_rcnn import FastRCNNPredictor

from .engine import train_one_epoch, evaluate

from .import utils
from .dataset import Dataset, get_transform
from ..data_utils import DATA_ROOT, TRAIN_ROOT, TEST_ROOT, load_train_valid_df

import matplotlib.pyplot as plt
import numpy as np
from torchvision.utils import make_grid, draw_bounding_boxes
from torchvision.transforms import ToPILImage
import torchvision.transforms.functional as F

import sys

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    arg = parser.add_argument

    arg('--model', default='fasterrcnn_resnet50_fpn', help='model')
    arg('--device', default='cuda', help='device')
    arg('--batch-size', default=10, type=int) #12
    arg('--workers', default=4, type=int,
        help='number of data loading workers')
    arg('--lr', default=0.01, type=float, help='initial learning rate')
    arg('--momentum', default=0.9, type=float, help='momentum')
    arg('--wd', '--weight-decay', default=1e-4, type=float,
        help='weight decay (default: 1e-4)', dest='weight_decay')
    arg('--epochs', default=50, type=int, #100
        help='number of total epochs to run')
    arg('--lr-steps', default=[35], nargs='+', type=int,
        help='decrease lr every step-size epochs')
    arg('--lr-gamma', default=0.1, type=float,
        help='decrease lr by a factor of lr-gamma')
    arg('--cosine', type=int, default=0,
        help='cosine lr schedule (disabled step lr schedule)')
    arg('--print-freq', default=100, type=int, help='print frequency')
    arg('--output-dir', help='path where to save')
    arg('--resume', help='resume from checkpoint')
    arg('--test-only', help='Only test the model', action='store_true')
    arg('--submission', help='Create test predictions', action='store_true')
    arg('--pretrained', type=int, default=0,
        help='Use pre-trained models from the modelzoo')
    arg('--score-threshold', type=float, default=0.5)
    arg('--nms-threshold', type=float, default=0.25)
    arg('--repeat-train-step', type=int, default=2)

    # fold parameters
    arg('--fold', type=int, default=0)
    arg('--n-folds', type=int, default=5)

    # distributed training parameters
    arg('--world-size', default=1, type=int,
        help='number of distributed processes')
    arg('--dist-url', default='env://',
        help='url used to set up distributed training')

    args = parser.parse_args()
    if args.test_only and args.submission:
        parser.error('pass one of --test-only and --submission')

    output_dir = Path(args.output_dir) if args.output_dir else None
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)

    utils.init_distributed_mode(args)
    print(args)

    device = torch.device(args.device)

    # Data loading code
    print('Loading data')

    df_train, df_valid = load_train_valid_df(args.fold, args.n_folds)
    print("train:",len(df_train))
    print("valid:",len(df_valid))
    #df_train.to_excel("df_train.xlsx")
    #print("data exported!")
    root = TRAIN_ROOT
    if args.submission:
        Check_testData= True #Bool value to validate the F1 score in testdata
        df_valid = pd.read_csv(DATA_ROOT / 'sample_submission.csv')
        #print(df_valid)
        if not Check_testData:
            df_valid['labels'] = ''
            
        root = TEST_ROOT
    dataset = Dataset(
        df_train, get_transform(train=True), root, skip_empty=False)
    dataset_test = Dataset(
        df_valid, get_transform(train=False), root, skip_empty=False)
    print("Test:",dataset_test.__len__())

    print('Creating data loaders')
    if args.distributed:
        train_sampler = torch.utils.data.distributed.DistributedSampler(dataset)
        test_sampler = \
            torch.utils.data.distributed.DistributedSampler(dataset_test)
    else:
        #Ramdom data augmenting is created, same size as train (2948) and test (657) dataset
        train_sampler = torch.utils.data.RandomSampler(dataset)
        test_sampler = torch.utils.data.SequentialSampler(dataset_test)
        #print(len(test_sampler))

    train_batch_sampler = torch.utils.data.BatchSampler(
        train_sampler, args.batch_size, drop_last=True)
    #print("CCC", (train_batch_sampler))
    data_loader = torch.utils.data.DataLoader(
        dataset, batch_sampler=train_batch_sampler, num_workers=args.workers,
        collate_fn=utils.collate_fn)
    #print("AAA",len(data_loader)) #Train batch of 184*16=2948
    #print("train", len(data_loader))
    
    data_loader_test = torch.utils.data.DataLoader(
        dataset_test, batch_size=1,
        sampler=test_sampler, num_workers=args.workers,
        collate_fn=utils.collate_fn)
    #print("BBB", len(data_loader_test))  #Test same size= 657
    #show_batch(data_loader)
    #print("test2:", len(data_loader_test))
    
    # stop=1
    # if stop:
    #     wait = input("Press Enter to continue....")
    #     sys.exit()
    
    print('Creating model')
    model = build_model(args.model, args.pretrained, args.nms_threshold)
    model.to(device)

    model_without_ddp = model
    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(
            model, device_ids=[args.gpu])
        model_without_ddp = model.module

    params = [p for p in model.parameters() if p.requires_grad]
    optimizer = torch.optim.SGD(
        params, lr=args.lr, momentum=args.momentum,
        weight_decay=args.weight_decay)

    lr_scheduler = None
    if args.cosine:
        lr_scheduler = CosineAnnealingLR(optimizer, args.epochs)
    elif args.lr_steps:
        lr_scheduler = MultiStepLR(
            optimizer, milestones=args.lr_steps, gamma=args.lr_gamma)

    if args.resume:
        checkpoint = torch.load(args.resume, map_location='cpu')
        if 'model' in checkpoint:
            model_without_ddp.load_state_dict(checkpoint['model'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            if lr_scheduler and 'lr_scheduler' in checkpoint:
                lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])
        else:
            model_without_ddp.load_state_dict(checkpoint)
        print(f'Loaded from checkpoint {args.resume}')

    # stop=1
    # if stop:
    #     wait = input("Press Enter to continue....")
    #     sys.exit()
    
    def save_eval_results(er):
        scores, clf_gt = er
        if output_dir:
            pd.DataFrame(scores).to_csv(output_dir / 'eval.csv', index=None)
            pd.DataFrame(clf_gt).to_csv(output_dir / 'clf_gt.csv', index=None)

    if args.test_only or args.submission:
        _, eval_results = evaluate(
            model, data_loader_test, device=device, output_dir=output_dir,
            threshold=args.score_threshold)
        if args.test_only:
            save_eval_results(eval_results)
        elif output_dir:
            #segmentation BBoxes and labels Test
            pd.DataFrame(eval_results[1]).to_csv(
                output_dir / 'test_predictions.csv', index=None)
            #scores Test
            pd.DataFrame(eval_results[0]).to_csv(
                output_dir / 'test_eval.csv', index=None)
        return

    print('Start training')
    best_f1 = 0
    start_time = time.time()
    for epoch in range(args.epochs):
        if args.distributed:
            train_sampler.set_epoch(epoch)
        for _ in range(args.repeat_train_step):
            train_metrics = train_one_epoch(
                model, optimizer, data_loader, device, epoch, args.print_freq)
        if lr_scheduler:
            lr_scheduler.step()
        if output_dir:
            json_log_plots.write_event(output_dir, step=epoch, **train_metrics)
            utils.save_on_master({
                'model': model_without_ddp.state_dict(),
                'optimizer': optimizer.state_dict(),
                'lr_scheduler': (
                    lr_scheduler.state_dict() if lr_scheduler else None),
                'args': args},
                output_dir / 'checkpoint.pth')

        # evaluate after every epoch
        eval_metrics, eval_results = evaluate(
            model, data_loader_test, device=device, output_dir=None,
            threshold=args.score_threshold)
        save_eval_results(eval_results)
        if output_dir:
            json_log_plots.write_event(output_dir, step=epoch, **eval_metrics)
            if eval_metrics['f1'] > best_f1:
                best_f1 = eval_metrics['f1']
                print(f'Updated best model with f1 of {best_f1}')
                utils.save_on_master(
                    model_without_ddp.state_dict(),
                    output_dir / 'model_best.pth')

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('Training time {}'.format(total_time_str))


def build_model(name: str, pretrained: bool, nms_threshold: float):
    anchor_sizes = [12, 24, 32, 64, 96]
    model = detection.__dict__[name](
        pretrained=pretrained,
        rpn_anchor_generator=AnchorGenerator(
            sizes=tuple((s,) for s in anchor_sizes),
            aspect_ratios=tuple((0.5, 1.0, 2.0) for _ in anchor_sizes),
        ),
        box_detections_per_img=1000,
        box_nms_thresh=nms_threshold,
    )
    model.roi_heads.box_predictor = FastRCNNPredictor(
        in_channels=model.roi_heads.box_predictor.cls_score.in_features,
        num_classes=2)
    model.transform = ModelTransform(
        image_mean=model.transform.image_mean,
        image_std=model.transform.image_std,
    )
    return model

def show(imgs):
    fix, axs = plt.subplots(nrows=2, ncols=int(len(imgs)/2))
    plt.subplots_adjust(top=1.0, bottom=0.0, left=0.0, right=1.0, hspace=0.0, wspace=0.0)
    for i, img in enumerate(imgs):
        img = img.detach()
        img = F.to_pil_image(img)
        if i < len(imgs)/2:
            axs[0, i].imshow(np.asarray(img))
            axs[0, i].set(xticklabels=[], yticklabels=[], xticks=[], yticks=[])
        else:
            axs[1, int(i-len(imgs)/2)].imshow(np.asarray(img))
            axs[1, int(i-len(imgs)/2)].set(xticklabels=[], yticklabels=[], xticks=[], yticks=[])
    plt.show()
        
def show_batch(dl):
    dataiter = iter(dl)
    images_batch, labels = dataiter.next()
    #print(labels[0]['boxes'])
    #print("aaa",(images_batch)) #16,[0]=3, [tensor]
    all_boxes=[]
    for i in range(len(images_batch)):
        img= images_batch[i].detach()
        out_img_tensor = img * 255
        out_img_type = out_img_tensor.to(torch.uint8) #.type(torch.uint8)
        drawn_boxes= draw_bounding_boxes(out_img_type, labels[i]['boxes'], colors="red")
        all_boxes.append(drawn_boxes)
    #Show grid without bboxes:
    #grid= make_grid(torch.stack(images_batch))
    #img= ToPILImage()((grid))
    #img.show()
    #Show grid w/bboxes:
    show(all_boxes)
    
    #show grid w/bboxes for one picture!
    # one_img= images_batch[0].detach()
    # out_img_tensor = one_img * 255
    # out_img_type = out_img_tensor.to(torch.uint8) #.type(torch.uint8)
    # drawn_boxes= ToPILImage()(draw_bounding_boxes(out_img_type, labels[0]['boxes'], colors="red"))
    # drawn_boxes.show()

class ModelTransform(GeneralizedRCNNTransform):
    def __init__(self, image_mean, image_std):
        nn.Module.__init__(self)
        self.image_mean = image_mean
        self.image_std = image_std

    def resize(self, image, target):
        return image, target


if __name__ == '__main__':
    print('hola')
    #main()

# bash ./segmentation.sh