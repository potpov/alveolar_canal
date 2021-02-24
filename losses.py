import numpy as np
import torch
from torch import nn
import torch.nn.functional as F


class DiceLoss(nn.Module):
    def __init__(self, classes, device):
        super().__init__()
        self.eps = 1e-06
        self.classes = classes
        self.device = device

    def forward(self, pred, gt):
        included = torch.Tensor([k not in ['BACKGROUND', 'UNLABELED'] for k, v in self.classes.items()]).bool()

        gt_onehot = one_hot_encode(gt, pred.shape, self.device).permute(0, 4, 1, 2, 3)
        input_soft = F.softmax(pred, dim=1)
        dims = (2, 3, 4)
        intersection = torch.sum(input_soft * gt_onehot, dims)
        cardinality = torch.sum(input_soft + gt_onehot, dims)
        dice_score = 2. * intersection / (cardinality + self.eps)
        return torch.mean(1. - dice_score[:, included])


def one_hot_encode(volume, shape, device):
    B, C, Z, H, W = shape
    flat = volume.reshape(-1).unsqueeze(dim=1)  # 1xB*Z*H*W
    onehot = torch.zeros(size=(B * Z * H * W, C), dtype=torch.float).to(device)  # 1xB*Z*H*W destination tensor
    onehot.scatter_(1, flat, 1)  # writing the conversion in the destination tensor
    return torch.squeeze(onehot).reshape(B, Z, H, W, C)  # reshaping to the original shape


class LossFn:
    def __init__(self, loss_config, loader_config, weights):

        if not isinstance(loss_config['name'], list):
            self.name = [loss_config['name']]
        else:
            self.name = loss_config['name']
        self.loader_config = loader_config
        self.classes = loader_config['labels']
        self.weights = weights

    def factory_loss(self, pred, gt, name, warmup):

        if name == 'CrossEntropyLoss':
            # sigmoid here which is included in other losses
            pred = torch.nn.Sigmoid()(pred)
            loss_fn = nn.CrossEntropyLoss(weight=self.weights).to(self.device)
        elif name == 'BCEWithLogitsLoss':
            # one hot encoding for cross entropy with digits. Bx1xHxW -> BxCxHxW
            B, C, Z, H, W = pred.shape
            gt_flat = gt.reshape(-1).unsqueeze(dim=1)  # 1xB*Z*H*W

            gt_onehot = torch.zeros(size=(B * Z * H * W, C), dtype=torch.float).to(self.device)  # 1xB*Z*H*W destination tensor
            gt_onehot.scatter_(1, gt_flat, 1)  # writing the conversion in the destination tensor

            gt = torch.squeeze(gt_onehot).reshape(B, Z, H, W, C)  # reshaping to the original shape
            pred = pred.permute(0, 2, 3, 4, 1)  # for BCE we want classes in the last axis
            loss_fn = nn.BCEWithLogitsLoss(pos_weight=self.weights).to(self.device)
        elif name == 'DiceLoss':
            # pred = torch.argmax(torch.nn.Softmax(dim=1)(pred), dim=1)
            # pred = pred.data.cpu().numpy()
            # gt = gt.cpu().numpy()
            return DiceLoss(self.classes, self.device)(pred, gt)
        else:
            raise Exception("specified loss function cant be found.")

        return loss_fn(pred, gt)

    def __call__(self, pred, gt, warmup):
        """
        SHAPE MUST BE Bx1xHxW
        :param pred:
        :param gt:
        :return:
        """
        assert pred.device == gt.device
        assert gt.device != 'cpu'
        self.device = pred.device

        cur_loss = []
        for name in self.name:
            loss = self.factory_loss(pred, gt, name, warmup)
            if torch.isnan(loss):
                raise ValueError('Loss is nan during training...')
            cur_loss.append(loss)
        return torch.sum(torch.stack(cur_loss))
