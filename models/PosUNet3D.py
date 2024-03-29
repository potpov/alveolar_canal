import torch
import torch.nn as nn

class PosUNet3D(nn.Module):

    def __init__(self, n_classes, emb_shape):
        self.n_classes = n_classes
        super(PosUNet3D, self).__init__()
        Z, H, W = emb_shape
        feature_emb = Z * H * W

        emb_channels = 512

        # replace 1 with emb_voxel and emb_channels with 1 to have a weight for each emb_voxel instead of channels
        self.pos_emb_layer = nn.Linear(6, emb_channels)  # (pos_size, emb_channels, 1)

        self.ec0 = self.conv3Dblock(3, 32)
        self.ec1 = self.conv3Dblock(32, 64, kernel_size=3, padding=1)  # third dimension to even val
        self.ec2 = self.conv3Dblock(64, 64)
        self.ec3 = self.conv3Dblock(64, 128)
        self.ec4 = self.conv3Dblock(128, 128)
        self.ec5 = self.conv3Dblock(128, 256)
        self.ec6 = self.conv3Dblock(256, 256)
        self.ec7 = self.conv3Dblock(256, emb_channels)

        self.pool0 = nn.MaxPool3d(2)
        self.pool1 = nn.MaxPool3d(2)
        self.pool2 = nn.MaxPool3d(2)

        self.dc9 = nn.ConvTranspose3d(512, 512, kernel_size=2, stride=2)
        self.dc8 = self.conv3Dblock(256 + 512, 256, kernel_size=3, stride=1, padding=1)
        self.dc7 = self.conv3Dblock(256, 256, kernel_size=3, stride=1, padding=1)
        self.dc6 = nn.ConvTranspose3d(256, 256, kernel_size=2, stride=2)
        self.dc5 = self.conv3Dblock(128 + 256, 128, kernel_size=3, stride=1, padding=1)
        self.dc4 = self.conv3Dblock(128, 128, kernel_size=3, stride=1, padding=1)
        self.dc3 = nn.ConvTranspose3d(128, 128, kernel_size=2, stride=2)
        self.dc2 = self.conv3Dblock(64 + 128, 64, kernel_size=3, stride=1, padding=1)
        self.dc1 = self.conv3Dblock(64, 64, kernel_size=3, stride=1, padding=1)
        self.final = nn.ConvTranspose3d(64, n_classes, kernel_size=3, padding=1, stride=1)

    def conv3Dblock(self, in_channels, out_channels, kernel_size=(3, 3, 3), stride=1, padding=(1, 1, 1)):
        return nn.Sequential(
                nn.Conv3d(in_channels, out_channels, kernel_size, stride=stride, padding=padding),
                nn.BatchNorm3d(out_channels),
                nn.ReLU()
        )

    def forward(self, x, position):
        h = self.ec0(x)
        feat_0 = self.ec1(h)
        h = self.pool0(feat_0)
        h = self.ec2(h)
        feat_1 = self.ec3(h)

        h = self.pool1(feat_1)
        h = self.ec4(h)
        feat_2 = self.ec5(h)

        h = self.pool2(feat_2)
        h = self.ec6(h)
        h = self.ec7(h)

        pos_emb = self.pos_emb_layer(position)
        h = self.attention(h, pos_emb.view(*pos_emb.shape, 1))

        h = torch.cat((self.dc9(h), feat_2), dim=1)

        h = self.dc8(h)
        h = self.dc7(h)

        h = torch.cat((self.dc6(h), feat_1), dim=1)
        h = self.dc5(h)
        h = self.dc4(h)

        h = torch.cat((self.dc3(h), feat_0), dim=1)
        h = self.dc2(h)
        h = self.dc1(h)
        return self.final(h.float())
