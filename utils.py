from torch.utils.data import DistributedSampler
import pathlib
import torch.utils.data as data
from torch.utils.data import SubsetRandomSampler
import torchio as tio
import logging
import os
import numpy as np
from os import listdir
from os.path import isfile, join
import re
import cv2
import yaml
from models.PadUNet2D import UNet as PadUNet2D
from models.PadUNet3D import padUNet3D
from models.transUnet import TransUNet3D
from models.PadUNet3D import PositionalpadUNet3D as PospadUNet3D
from models.TransBTS.TransBTS_downsample8x_skipconnection import TransBTS
from models.ResidualEncoder import ResNetEncoder
from models.ResNet50.ResNet50 import ResNet50
from models.Multiscale.Multiscale import Multiscale3D
from models.Competitor import Competitor
import sys
from Jaw import Jaw
import torch
from tqdm import tqdm
import SimpleITK as sitk
import json


def create_split(dataset_path):
    folder_debug = {'train': [], 'test': [], 'val': []}
    patients = os.listdir(dataset_path)
    tot_patients = len(patients)
    patients_ids = np.arange(tot_patients)
    np.random.shuffle(patients_ids)
    test_ids = patients_ids[:int(tot_patients * 0.2)]
    val_ids = patients_ids[int(tot_patients * 0.2):int(tot_patients * 0.3)]

    for patient_num, folder in (enumerate(patients)):
        partition = 'train'
        if patient_num in test_ids:
            partition = 'test'
        elif patient_num in val_ids:
            partition = 'val'
        folder_debug[partition].append(folder)

    json = json.dumps(folder_debug)
    f = open("configs/splits.json", "w")
    f.write(json)
    f.close()


def data_from_dicom(directory):
    folders = listdir(directory)
    for i, (folder) in tqdm(enumerate(folders), total=len(folders), desc='creating the gorgeous dataset'):
        TARGET_FOLDER = os.path.join(directory, folder, 'DICOM', 'DICOMDIR')
        if os.path.exists(TARGET_FOLDER):
            j = Jaw(TARGET_FOLDER)
            np.save(os.path.join(directory, folder, 'data.npy'), j.get_volume())


def fix_dataset_folder(directory):
    final_files = ['data.npy', 'DICOM', 'gt_2labels.npy', 'gt_4labels.npy']
    for folder in listdir(directory):
        files = os.listdir(os.path.join(directory, folder))
        assert 'DICOM' in files, 'dicom folder not found'

        if all(file in final_files for file in files):  # this folder is okay
            continue

        # better file format (from DICOM LUT)
        data = Jaw(os.path.join(directory, folder, 'DICOM', 'DICOMDIR')).get_volume()
        four_labels = np.load(os.path.join(directory, folder, 'gt_volume.npy'))
        two_labels = convert_to_two_labels(four_labels)

        np.save(os.path.join(directory, folder, 'data.npy'), data)
        os.remove(os.path.join(directory, folder, 'volume.npy'))  # remove old useless volume
        os.rename(os.path.join(directory, folder, 'gt_volume.npy'), os.path.join(directory, folder, 'gt_4labels.npy'))
        np.save(os.path.join(directory, folder, 'gt_2labels.npy'), two_labels)

        # final check
        assert all(file in final_files for file in listdir(os.path.join(directory, folder)))


def convert_to_two_labels(volume):
    """
    WARNING: HARD CODED LABELS: USE THIS TOOL AT YOUR OWN RISK!
    :param volume: ground truth volume to convert
    :return: converted ground truth volume
    """
    volume[volume == 0] = 1
    volume[volume == 3] = 2
    volume[volume == 2] = 0
    return volume


def npy_maker(path):
    """
    creating a npy file from a set of mask images.
    obsolete with tool version 1.1
    :param path: path to the image folder
    """
    idxs = [
            int(re.search('(\d{1,5})_mask.png', filename).group(1))
            for filename in listdir(join(path, 'mask'))
            if isfile(join(path, 'mask', filename))
        ]
    idxs.sort()
    np.save(
        join(path, 'gt.npy'),
        np.stack([cv2.imread(join(path, 'mask', '{}_mask.png'.format(idx))) for idx in idxs])
    )
    np.save(
        join(path, 'data.npy'),
        np.stack([cv2.imread(join(path, 'img', '{}_img.png'.format(idx))) for idx in idxs])
    )


def set_logger(log_path=None):
    """
    Set the logger to log info in terminal and file `log_path`.
    In general, it is useful to have a logger so that every output to the terminal is saved
    in a permanent file. Here we save it to `model_dir/train.log`.
    """
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        if not log_path:
            # Logging to console
            stream_handler = logging.StreamHandler(sys.stdout)
            stream_handler.setFormatter(logging.Formatter('%(message)s'))
            logger.addHandler(stream_handler)
        else:
            # Logging to a file
            file_handler = logging.FileHandler(os.path.join(log_path))
            file_handler.setFormatter(logging.Formatter('%(asctime)s:%(levelname)s: %(message)s'))
            logger.addHandler(file_handler)


def load_config_yaml(config_file):
    return yaml.load(open(config_file, 'r'), yaml.FullLoader)


def load_model(config):

    model_config = config.get('model')
    loader_config = config.get('data-loader')
    loss_config = config.get('loss')

    if 'Jaccard' in loss_config['name'] and len(loader_config['labels']) <= 2:
        num_classes = 1
    else:
        num_classes = len(loader_config['labels'])

    name = model_config.get('name', 'UNet3D')

    if name == 'PadUNet2D':
        return PadUNet2D(num_classes=num_classes, in_ch=3), "2D"

    emb_shape = [dim // 8 for dim in loader_config['patch_shape']]

    if name == 'PadUNet3D':
        return padUNet3D(n_classes=num_classes), "3D"
    elif name == 'PosPadUNet3D':
        return PospadUNet3D(n_classes=num_classes, emb_shape=emb_shape), "3D"
    elif name == 'transBTS':
        _, net = TransBTS(num_classes=num_classes, img_dim=loader_config['patch_shape'][0])
        return net,"3D"
    elif name == 'transUNet3D':
        return TransUNet3D(n_classes=num_classes, emb_shape=emb_shape), "3D"
    elif name == 'Multiscale':
        return Multiscale3D(num_classes=num_classes), "3D"
    elif model_config['name'] == 'RESNET18':
        return ResNetEncoder(n_classes=num_classes), "3D"
    elif model_config['name'] == 'RESNET50':
        return ResNet50(out_channels=num_classes), "3D"
    elif model_config['name'] == 'Competitor':
        return Competitor(n_classes=num_classes), "3D"
    else:
        raise Exception("Model not found, check the config.yaml")



##########################
#   BACKGROUND SUPPRESSION

def compute_skeleton(img):
    """
    create the skeleton using morphology
    Args:
        img (numpy array): source image

    Returns:
        (numpy array), b&w image: 0 background, 255 skeleton elements
    """

    img = img.astype(np.uint8)
    size = img.size
    skel = np.zeros(img.shape, np.uint8)
    kernel = cv2.getStructuringElement(cv2.MORPH_CROSS, (3, 3))
    while True:
        eroded = cv2.erode(img, kernel)
        temp = cv2.dilate(eroded, kernel)
        temp = cv2.subtract(img, temp)
        skel = np.bitwise_or(skel, temp)
        img = eroded.copy()
        zeros = size - cv2.countNonZero(img)
        if zeros == size:
            return skel


def fill_holes(img):
    assert np.array_equal(img, img.astype(bool)), "not binary image provided in hole filling"
    _, labels, stats, _ = cv2.connectedComponentsWithStats(img.astype(np.int8))
    major_label = np.argsort(-stats[1:, -1])[0] + 1
    return (labels == major_label).astype(np.int8)


def arch_detection(slice):
    """
    compute a polynomial spline of the dental arch from a DICOM file
    Args:
        slice (numpy array): source image. Must be float with values in range [0,1]

    Returns:
        (poly1d object): polynomial function approximation
        (float) starting value for the X axis
        (float) ending value for the X axis
    """

    # initial closing
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    arch = cv2.morphologyEx(slice, cv2.MORPH_CLOSE, kernel)

    # thresholding to find the dental arch
    values, bins = np.histogram(arch, bins=40)
    cumulative = np.cumsum(values) / arch.size  # normalized cumulative hist values
    suitable_idx = np.abs(cumulative - 0.86).argmin()  # suitable th idx for this slice
    arch = cv2.threshold(arch, bins[suitable_idx], 1, cv2.THRESH_BINARY)[1]

    # removing external noise and internal holes with labelling
    arch = fill_holes(arch.astype(np.int8))
    arch = 1 - fill_holes(1 - arch)

    # compute skeleton
    skel = compute_skeleton(arch)

    # regression polynomial function
    coords = np.argwhere(skel > 0)
    y, x = list(coords[:, 0]), list(coords[:, 1])
    try:
        pol = np.polyfit(x, y, 8)
        p = np.poly1d(pol)
    except np.RankWarning:
        pass

    # DEBUG PREDICTED SPLINE
    # original_rgb = np.tile(slice, (3, 1, 1))  # overlay on the original image (colorful)
    # original_rgb = np.moveaxis(original_rgb, 0, -1)
    # for sample in np.linspace(min(x), max(x), 1000):  # range(min(x), max(x)):
    #     y_sample = p(sample)
    #     try:
    #         original_rgb[int(y_sample), int(sample), :] = (1, 0, 0)
    #     except IndexError as e:
    #         pass
    # plt.imshow(original_rgb, cmap='gray')
    # plt.show()
    # END DEBUG

    return p, min(x), max(x)


def arch_stats(func, start, end):
    x = start
    counter = 0
    delta = 0.3
    peak = 100
    while x < end:
        y = func(x)
        peak = peak if y > peak else y  # this is not a bug, peak value is the lowest
        alfa = (func(x + delta / 2) - func(x - delta / 2)) / delta
        x = x + 1 * np.sqrt(1 / (alfa ** 2 + 1))
        counter = counter + 1
    return counter, peak


def paralines_mask(func, start, end, slice_dim, offset=50):
    """
    this functions uses the first order derivative of the function func to track the proper points (x,y) from start to end.
    Args:
        func (poly1d object): polynomial function approximation
        end (float) starting value for the X axis
        start (float) ending value for the X axis
        offset (Int): offset for generating two more curves

    Returns:
        low_offset (numpy array): set of sets of xy coordinates (lower offset)
        coords (numpy array): set of sets of xy coordinates
        high_offset (numpy array): set of sets of xy coordinates (higer offset)
        derivative: set of derivates foreach point of coords
    """

    d = 1
    delta = 0.3
    hx, hy = [], []
    lx, ly = [], []
    x = start + 1

    while x < end:
        y = func(x)
        alfa = (func(x + delta / 2) - func(x - delta / 2)) / delta

        # computing the shift trajectory
        ort_alpha = -1 / alfa
        cos = np.sqrt(1 / (ort_alpha ** 2 + 1))
        sin = np.sqrt(ort_alpha ** 2 / (ort_alpha ** 2 + 1))
        if ort_alpha > 0:
            lx.append(x + offset * cos), ly.append(y + offset * sin)
            hx.append(x - offset * cos), hy.append(y - offset * sin)
        else:
            lx.append(x - offset * cos), ly.append(y + offset * sin)
            hx.append(x + offset * cos), hy.append(y - offset * sin)

        x = x + d * np.sqrt(1 / (alfa ** 2 + 1))  # moving to the next x-axis val to check

    H, W = slice_dim
    hp = np.poly1d(np.polyfit(hx, hy, 6))
    lp = np.poly1d(np.polyfit(lx, ly, 6))

    mask = np.zeros((H, W))
    start = max(int(hx[0]), 0)
    end = min(int(hx[-1]), W - 1)
    hy2 = [hp(x) for x in range(start, end)]
    ly2 = [lp(x) for x in range(start, end)]

    hy2 = np.clip(hy2, a_min=0, a_max=H - 1)
    ly2 = np.clip(ly2, a_min=0, a_max=H - 1)
    for idx in range(start, end):
        mask[int(hy2[idx - start]):int(ly2[idx - start]), int(idx)] = 1

    # mask = np.zeros((H, W))
    # hy = [hp(x) for x in range(start_x, end_x)]
    # ly = [lp(x) for x in range(start_x, end_x)]
    #
    # hy = np.clip(hy, a_min=0, a_max=H - 1)
    # ly = np.clip(ly, a_min=0, a_max=H - 1)
    # for idx in range(start_x, end_x):
    #     mask[int(hy[idx - start_x]):int(ly[idx - start_x]), int(idx)] = 1

    return mask.astype(np.bool)


def arch_lines(func, start, end, offset=50):
    """
    this functions uses the first order derivative of the function func to track the proper points (x,y) from start to end.
    Args:
        func (poly1d object): polynomial function approximation
        end (float) starting value for the X axis
        start (float) ending value for the X axis
        offset (Int): offset for generating two more curves

    Returns:
        low_offset (numpy array): set of sets of xy coordinates (lower offset)
        coords (numpy array): set of sets of xy coordinates
        high_offset (numpy array): set of sets of xy coordinates (higer offset)
        derivative: set of derivates foreach point of coords
    """

    d = 1
    delta = 0.3
    coords = []
    x = start + 1
    # we start from the range of X values on the X axis,
    # we create a new list X of x coords along the curve
    # we exploit the first order derivative to place values in X
    # so that f(X) is equally distant for each point in X
    while x < end:
        coords.append((x, func(x)))
        alfa = (func(x + delta / 2) - func(x - delta / 2)) / delta
        x = x + d * np.sqrt(1 / (alfa ** 2 + 1))

    # creating lines parallel to the spline
    high_offset = []
    low_offset = []
    derivative = []
    for x, y in coords:
        alfa = (func(x + delta / 2) - func(x - delta / 2)) / delta  # first derivative
        alfa = -1 / alfa  # perpendicular coeff
        cos = np.sqrt(1 / (alfa ** 2 + 1))
        sin = np.sqrt(alfa ** 2 / (alfa ** 2 + 1))
        if alfa > 0:
            low_offset.append((x + offset * cos, y + offset * sin))
            high_offset.append((x - offset * cos, y - offset * sin))
        else:
            low_offset.append((x - offset * cos, y + offset * sin))
            high_offset.append((x + offset * cos, y - offset * sin))
        derivative.append(alfa)

    return low_offset, coords, high_offset, derivative


def background_suppression(data, folder):
    """
    detect the best spline from a set of 40 central slices of the volume,
    draw the parallel splines to select the most relevant zone of the volume
    suppress all the data out of this zone
    :param data:
    :return:
    """

    slice_range = 40
    step = 4
    Z_center = data.shape[0] // 2
    best = 100
    slice_id = Z_center
    setup = []
    for i in range(Z_center - slice_range, Z_center + slice_range, step):
        p, start, end = arch_detection(data[i])
        mid = (start + end) // 2
        new_start = start + np.argmax([p(i) for i in range(start, mid)])  # removing possible noise at the beginning of the spline
        new_end = mid + np.argmax([p(i) for i in range(mid, end)])  # same as above for the end of the spline
        score = abs(p(new_start) - p(new_end))  # best spline starts and ends at the same level of depth
        lenght, peak = arch_stats(p, new_start, new_end)
        if new_start < 100 and new_end > data.shape[-1] - 100 and p(new_start) > data.shape[-2] - 200 and p(new_end) > data.shape[-2] - 200:
            if score < best and lenght > 500 and peak < 80:
                # print(f"this is a new best: {score}, {lenght}, {peak}")
                best = score
                slice_id = i
                setup = [p, new_start, new_end]

    if len(setup) == 0:
        print(f"found patient {folder} where preprocessing was not feasible.")
        return data

    f, start, end = setup
    mask = paralines_mask(f, start, end, slice_dim=(data.shape[-2:]), offset=40)
    # suppressing data below the lowest point in the spline
    minimum = f(start) if f(start) > f(end) else f(end)
    mask[int(minimum):, :] = False

    data[:, np.bitwise_not(mask)] = 0  # using mask to suppress data

    # # DEBUG: check the result
    # save_dir = os.path.join(r'C:\Users\marco\Desktop\pre-processing-risultati\test_currently')
    # original_rgb = np.tile(data[slice_id], (3, 1, 1))  # overlay on the original image (colorful)
    # original_rgb = np.moveaxis(original_rgb, 0, -1)
    # original_rgb *= 255
    # for sample in np.linspace(start, end, 1000):  # range(min(x), max(x)):
    #     y_sample = f(sample)
    #     try:
    #         original_rgb[int(y_sample), int(sample), :] = (255, 0, 0)
    #     except IndexError as e:
    #         pass
    # plt.imshow(original_rgb.astype(np.int))
    # plt.savefig(os.path.join(save_dir, '{}.png'.format(folder)))
    # END DEBUG

    return data

#   END BACKGROUND SUPPRESSION
##############################

class Splitter:
    def __init__(self, split):
        self.nz, self.nh, self.nw = split
        self.batch_size = self.nz * self.nh * self.nw

    def split(self, data):
        splits = []
        for wid, wsub in enumerate(np.array_split(data, self.nw, 2)):
            for hid, hsub in enumerate(np.array_split(wsub, self.nh, 1)):
                for zid, zsub in enumerate(np.array_split(hsub, self.nz, 0)):
                    splits.append(zsub)
        return splits

    def merge(self, splits):
        assert len(splits) == self.batch_size
        on_x = []
        for i in range(0, self.nw):
            on_y = []
            for j in range(self.nh):
                on_z = []
                for k in range(self.nz):
                    on_z.append(splits[i * self.nz * self.nh + j * self.nz + k])
                on_y.append(torch.cat(on_z, dim=-3))
            on_x.append(torch.cat(on_y, dim=-2))
        return torch.cat(on_x, dim=-1)

    def get_batch(self):
        return self.batch_size


def load_dataset(config, rank, world_size, is_distributed, train_type="2D", is_competitor=False):
    from loaders.dataset2D import AlveolarDataloader
    from loaders.dataset3D import Loader3D
    loader_config = config.get('data-loader', None)
    train_config = config.get('trainer', None)

    train_loader, test_loader, val_loader = None, None, None

    if train_type == "2D":
        alveolar_data = AlveolarDataloader(loader_config, train_config.get("do_train", None), train_config.get("additional_dataset", None))
        train_id, test_id, val_id = alveolar_data.split_dataset()
        splitter = alveolar_data.get_splitter()
        if config['trainer']['do_train']:
            train_loader = data.DataLoader(
                alveolar_data,
                batch_size=loader_config['batch_size'],
                sampler=SubsetRandomSampler(train_id),
                num_workers=loader_config['num_workers'],
                pin_memory=True,
                drop_last=True,
            )
        if rank == 0:
            test_loader = data.DataLoader(
                alveolar_data,
                batch_size=loader_config['batch_size'],
                sampler=test_id,
                num_workers=loader_config['num_workers'],
                pin_memory=True,
                drop_last=False,
                collate_fn=alveolar_data.custom_collate
            )

            val_loader = data.DataLoader(
                alveolar_data,
                batch_size=loader_config['batch_size'],
                sampler=val_id,
                num_workers=loader_config['num_workers'],
                pin_memory=True,
                drop_last=False,
                collate_fn=alveolar_data.custom_collate
            )
    elif train_type == "3D":
        data_utils = Loader3D(loader_config, train_config.get("do_train", None), train_config.get("additional_dataset", None), is_competitor)
        train_d, test_d, val_d = data_utils.split_dataset()
        splitter = None

        if config['trainer']['do_train']:
            samples_per_volume = int(np.prod([np.round(i / j) for i, j in zip(loader_config['resize_shape'], loader_config['patch_shape'])]))
            train_queue = tio.Queue(
                train_d,
                max_length=samples_per_volume * 4,  # queue len
                samples_per_volume=samples_per_volume,
                sampler=data_utils.get_sampler(loader_config.get('sampler_type', 'grid'), loader_config.get('grid_overlap', 0)),
                num_workers=loader_config['num_workers'],
            )
            sampler = DistributedSampler(train_queue, shuffle=False) if is_distributed else None
            train_loader = data.DataLoader(train_queue, loader_config['batch_size'] // world_size, num_workers=0, sampler=sampler)

        if rank == 0:
            test_loader = [(test_p, data.DataLoader(test_p, loader_config['batch_size'], num_workers=loader_config['num_workers'])) for test_p in test_d]
            val_loader = [(val_p, data.DataLoader(val_p, loader_config['batch_size'], num_workers=loader_config['num_workers'])) for val_p in val_d]
    else:
        raise Exception(f"type {train_type} not recognized!")

    return train_loader, test_loader, val_loader, splitter


def resample(ctvol, is_label, original_spacing=.3, out_spacing=.4):
    original_spacing = (original_spacing, original_spacing, original_spacing)
    out_spacing = (out_spacing, out_spacing, out_spacing)

    ctvol_itk = sitk.GetImageFromArray(ctvol)
    ctvol_itk.SetSpacing(original_spacing)
    original_size = ctvol_itk.GetSize()
    out_shape = [int(np.round(original_size[0] * (original_spacing[0] / out_spacing[0]))),
                 int(np.round(original_size[1] * (original_spacing[1] / out_spacing[1]))),
                 int(np.round(original_size[2] * (original_spacing[2] / out_spacing[2])))]

    # Perform resampling:
    resample = sitk.ResampleImageFilter()
    resample.SetOutputSpacing(out_spacing)
    resample.SetSize(out_shape)
    resample.SetOutputDirection(ctvol_itk.GetDirection())
    resample.SetOutputOrigin(ctvol_itk.GetOrigin())
    resample.SetTransform(sitk.Transform())
    resample.SetDefaultPixelValue(ctvol_itk.GetPixelIDValue())

    if is_label:
        resample.SetInterpolator(sitk.sitkNearestNeighbor)
    else:
        resample.SetInterpolator(sitk.sitkBSpline)
    resampled_ctvol = resample.Execute(ctvol_itk)
    return sitk.GetArrayFromImage(resampled_ctvol)

if __name__ == '__main__':
    split_filepath = "/nas/softechict-nas-2/mcipriano/splits/main_train.json"
    with open(split_filepath) as f:
        folder_splits = json.load(f)

    # for split in ['train', 'syntetic', 'val']:
    for split in ['syntetic', 'val']:
        saving_dir = "/nas/softechict-nas-2/mcipriano/datasets/maxillo/competitor/"
        dirs = [os.path.join('/nas/softechict-nas-2/mcipriano/datasets/maxillo/SPARSE', p) for p in folder_splits[split]]

        dataset = {'data': [], 'gt': []}
        for i, dir in tqdm(enumerate(dirs), total=len(dirs), desc=f"processing {split}"):
            gt_dir = os.path.join(dir, 'syntetic.npy')
            data_dir = os.path.join(dir, 'data.npy')

            image = np.load(data_dir)
            gt = np.load(gt_dir)

            # rescale
            DICOM_MAX = 3095
            DICOM_MIN = 0
            image = np.clip(image, DICOM_MIN, DICOM_MAX)
            image = (image.astype(float) + DICOM_MIN) / (DICOM_MAX + DICOM_MIN)   # [0-1] with shifting

            image = resample(image, is_label=False)
            gt = resample(gt, is_label=True)

            s = tio.Subject(
                data=tio.ScalarImage(tensor=image[None]),
                label=tio.LabelMap(tensor=gt[None]),
            )

            grid_sampler = tio.inference.GridSampler(
                s,
                patch_size=(32, 32, 32),
                patch_overlap=(10, 10, 10),
            )

            patch_loader = torch.utils.data.DataLoader(grid_sampler, batch_size=1)
            for a in patch_loader:
                image = a['data'][tio.DATA].squeeze().numpy()
                gt = a['label'][tio.DATA].squeeze().numpy()
                if np.sum(gt) != 0:
                    dataset['data'].append(image)
                    dataset['gt'].append(gt)

        log_dir = pathlib.Path(os.path.join(saving_dir, split))
        log_dir.mkdir(parents=True, exist_ok=True)
        np.save(os.path.join(saving_dir, split, 'data.npy'), np.stack(dataset['data']))
        np.save(os.path.join(saving_dir, split, 'gt.npy'), np.stack(dataset['gt']))
        print(f"split {split} completed. created {len(dataset['data'])} subvolumes")

    print("subvolumes for training have been created!")
    # TODO: fare shuffle dei volumi
    # os.mkdir(os.path.join(saving_dir, 'test'))
    # # rescale
    # DICOM_MAX = 3095
    # DICOM_MIN = 0
    # image = np.clip(image, DICOM_MIN, DICOM_MAX)
    # image = (image.astype(float) + DICOM_MIN) / (DICOM_MAX + DICOM_MIN)  # [0-1] with shifting
    #
    # image = resample(image, is_label=False)
    # gt = resample(gt, is_label=True)

    # TODO: per test prendere i volumi e semplicemente fare resampling e salvarli in /vol_0.4
    # TODO: prima di salvarli fare clip