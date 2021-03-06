import os
import time
import numpy as np
import random
import cv2
from megengine.jit import trace, SublinearMemoryConfig
import megengine.distributed as dist
import megengine as mge
import megengine.functional as F
from edit.utils import imwrite, tensor2img, bgr2ycbcr, imrescale
from ..base import BaseModel
from ..builder import build_backbone
from ..registry import MODELS

def get_box(xy_ctr, offsets):
    """
        xy_ctr: [1,2,19,19]
        offsets: [B,2,19,19]
    """
    xy0 = (xy_ctr - offsets)  # top-left 用中心位置减去预测的偏移 得到左上角点
    xy1 = xy0 + 511  # bottom-right  用左上角点加上511得到右下角点
    bboxes_pred = F.concat([xy0, xy1], axis=1)  # (B,4,H,W) 将左上角和右下角点拼一起
    return bboxes_pred

config = SublinearMemoryConfig()

@trace(symbolic=True)
def train_generator_batch(optical, sar, label, *, opt, netG):
    netG.train()
    cls_score, offsets, ctr_score = netG(sar, optical)  #通过head得到分类分数、偏移和centerscore
    loss, loss_cls, loss_reg, loss_ctr = netG.loss(cls_score, offsets, ctr_score, label) #调用Loss函数计算loss
    opt.backward(loss)
    if dist.is_distributed():
        # do all reduce mean
        pass

    # performance in the training data 在训练数据上的性能
    B, _, _, _ = cls_score.shape #B,1,37,37
    cls_score = cls_score.reshape(B, -1)
    # find the max
    max_id = F.argmax(cls_score, axis = 1)  # (B, ) 找到最大的index
    pred_box = get_box(netG.fm_ctr, offsets)  # (B,4,H,W) 得到左上角点和右下角点
    pred_box = pred_box.reshape(B, 4, -1) #(B,4,H*W)
    output = []
    for i in range(B): #找到每个图片中 预测出来的最大位置的bbox框 存入output
        output.append(F.add_axis(pred_box[i, :, max_id[i]], axis=0)) # (1, 4)
    output = F.concat(output, axis=0)  # (B, 4)
    return [loss_cls, loss_reg, F.norm(output[:, 0:2] - label[:, 0:2], p=2, axis = 1).mean()] #只计算左上角点的差距


@trace(symbolic=True)
def test_generator_batch(optical, sar, *, netG):
    netG.eval()
    cls_score, offsets, ctr_score = netG(sar, optical)  # [B,1,19,19]  [B,2,19,19]  [B,1,19,19]
    B, _, _, _ = cls_score.shape
    # 加权
    # cls_score = cls_score * ctr_score
    cls_score = cls_score.reshape(B, -1)  # [B,19*19]
    # find the max
    max_id = F.argmax(cls_score, axis = 1)  # (B, )
    pred_box = get_box(netG.fm_ctr, offsets)  # (B,4,H,W)
    pred_box = pred_box.reshape(B, 4, -1)
    output = []
    for i in range(B):
        output.append(F.add_axis(pred_box[i, :, max_id[i]], axis=0)) # (1, 4)
    return F.concat(output, axis=0)  # [B,4]

def eval_distance(pred, gt):  # (4, )
    assert len(pred.shape) == 1
    return np.linalg.norm(pred[0:2]-gt[0:2], ord=2)

@MODELS.register_module()
class BasicMatching(BaseModel):
    allowed_metrics = {'dis': eval_distance}

    def __init__(self, generator, train_cfg=None, eval_cfg=None, pretrained=None):
        super(BasicMatching, self).__init__()

        self.train_cfg = train_cfg
        self.eval_cfg = eval_cfg

        # generator
        self.generator = build_backbone(generator)

        # load pretrained
        self.init_weights(pretrained)

    def init_weights(self, pretrained=None):
        """Init weights for models.

        Args:
            pretrained (str, optional): Path for pretrained weights. If given
                None, pretrained weights will not be loaded. Defaults to None.
        """
        self.generator.init_weights(pretrained)

    def train_step(self, batchdata):
        """train step.

        Args:
            batchdata: list for train_batch, numpy.ndarray, length up to Collect class.
        Returns:
            list: loss
        """
        optical, sar, label = batchdata
        # 保存optical 和 sar，看下对不对
        # name = random.sample('zyxwvutsrqponmlkjihgfedcba', 3)
        # name = "".join(name) + "_" + str(label[0][0]) + "_" + str(label[0][1]) + "_" + str(label[0][2]) + "_" + str(label[0][3])
        # imwrite(cv2.rectangle(tensor2img(optical[0, ...], min_max=(-0.64, 1.36)), (label[0][1], label[0][0]), (label[0][3], label[0][2]), (0,0,255), 2), file_path="./workdirs/" + name + "_opt.png") 
        # imwrite(tensor2img(sar[0, ...], min_max=(-0.64, 1.36)), file_path="./workdirs/" + name + "_sar.png")
        self.optimizers['generator'].zero_grad()
        loss = train_generator_batch(optical, sar, label, opt=self.optimizers['generator'], netG=self.generator) #调用得到loss
        self.optimizers['generator'].step()
        return loss

    def test_step(self, batchdata, **kwargs): #kwargs是调用时传进来的参数
        """test step.

        Args:
            batchdata: list for train_batch, numpy.ndarray or variable, length up to Collect class.

        Returns:
            list: outputs (already gathered from all threads)
        """
        optical = batchdata[0]  # [B ,1 , H, W]
        sar = batchdata[1]
        class_id = batchdata[2]
        file_id = batchdata[3]
        
        pre_bbox = test_generator_batch(optical, sar, netG=self.generator)  # [B, 4] 找到每张图像的预测框

        save_image_flag = kwargs.get('save_image')
        if save_image_flag:
            save_path = kwargs.get('save_path', None)
            start_id = kwargs.get('sample_id', None)
            if save_path is None or start_id is None:
                raise RuntimeError("if save image in test_step, please set 'save_path' and 'sample_id' parameters")
            
            with open(os.path.join(save_path, "result.txt"), 'a+') as f:
                for idx in range(pre_bbox.shape[0]):
                    # imwrite(tensor2img(optical[idx], min_max=(-0.64, 1.36)), file_path=os.path.join(save_path, "idx_{}.png".format(start_id + idx)))
                    # 向txt中加入一行
                    suffix = ".tif"
                    write_str = ""
                    write_str += str(class_id[idx])
                    write_str += " "
                    write_str += str(class_id[idx])
                    write_str += "_"
                    write_str += str(file_id[idx]) + suffix
                    write_str += " "
                    write_str += str(class_id[idx])
                    write_str += "_sar_"
                    write_str += str(file_id[idx]) + suffix
                    write_str += " "
                    write_str += str(int(pre_bbox[idx][1]*2+0.5))
                    write_str += " "
                    write_str += str(int(pre_bbox[idx][0]*2+0.5))
                    write_str += "\n"
                    f.write(write_str)

        return [pre_bbox, ]

    def cal_for_eval(self, gathered_outputs, gathered_batchdata):
        """

        :param gathered_outputs: list of variable, [pre_bbox, ]
        :param gathered_batchdata: list of numpy, [optical, sar, bbox_gt, class_id, file_id]
        :return: eval result
        """
        pre_bbox = gathered_outputs[0]
        bbox_gt = gathered_batchdata[2]
        class_id = gathered_batchdata[-2]
        file_id = gathered_batchdata[-1]
        assert list(bbox_gt.shape) == list(pre_bbox.shape), "{} != {}".format(list(bbox_gt.shape), list(pre_bbox.shape))

        res = []
        sample_nums = pre_bbox.shape[0]
        for i in range(sample_nums):
            eval_result = dict()
            for metric in self.eval_cfg.metrics:
                eval_result[metric] = self.allowed_metrics[metric](pre_bbox[i].numpy(), bbox_gt[i])
            eval_result['class_id'] = class_id[i]
            eval_result['file_id'] = file_id[i]
            res.append(eval_result)
        return res
