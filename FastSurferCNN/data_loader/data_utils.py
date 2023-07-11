# Copyright 2023 Image Analysis Lab, German Center for Neurodegenerative Diseases (DZNE), Bonn
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


# IMPORTS
from typing import Optional, Tuple, Union, Mapping

import numpy as np
from numpy import typing as npt
import torch
from skimage.measure import label, regionprops
from scipy.ndimage import (
    binary_erosion,
    binary_closing,
    filters,
    uniform_filter,
    generate_binary_structure,
)
import scipy.ndimage.morphology as morphology
import nibabel as nib
from nibabel.filebasedimages import FileBasedHeader as _Header
import pandas as pd

from FastSurferCNN.utils import logging
from FastSurferCNN.data_loader.conform import is_conform, conform, check_affine_in_nifti
from FastSurferCNN.utils.arg_types import VoxSizeOption

##
# Global Vars
##
SUPPORTED_OUTPUT_FILE_FORMATS = ["mgz", "nii", "nii.gz"]
LOGGER = logging.getLogger(__name__)

##
# Helper Functions
##


# Conform an MRI brain image to UCHAR, RAS orientation, and 1mm or minimal isotropic voxels
def load_and_conform_image(
        img_filename: str,
        interpol: int = 1,
        logger: logging.Logger = LOGGER,
        conform_min: bool = False
) -> Tuple[_Header, np.ndarray, np.ndarray]:
    """Function to load MRI image and conform it to UCHAR, RAS orientation and 1mm or minimum isotropic voxels size.
    Only, if it does not already have this format.

    Parameters
    ----------
    img_filename : str
        path and name of volume to read
    interpol : int
        interpolation order for image conformation (0=nearest,1=linear(default),2=quadratic,3=cubic)
    logger : logging.Logger
        Logger to write output to (default = STDOUT)
    conform_min : bool
        conform image to minimal voxel size (for high-res) (Default = False)

    Returns
    -------
    nibabel.Header header_info
        header information of the conformed image
    numpy.ndarray affine_info
        affine information of the conformed image
    numpy.ndarray orig_data
        conformed image data

    Raises
    ------
    RuntimeError
        Multiple input frames not supported
    RuntimeError
        Inconsistency in nifti-header



    """

    orig = nib.load(img_filename)
    # is_conform and conform accept numeric values and the string 'min' instead of the bool value
    _conform_vox_size = "min" if conform_min else 1.0
    if not is_conform(orig, conform_vox_size=_conform_vox_size):

        logger.info(
            "Conforming image to UCHAR, RAS orientation, and minimum isotropic voxels"
        )

        if len(orig.shape) > 3 and orig.shape[3] != 1:
            raise RuntimeError(
                f"ERROR: Multiple input frames ({orig.shape[3]}) not supported!"
            )

        # Check affine if image is nifti image
        if any(img_filename.endswith(ext) for ext in (".nii.gz", ".nii")):
            if not check_affine_in_nifti(orig, logger=logger):
                raise RuntimeError("ERROR: inconsistency in nifti-header. Exiting now.")

        # conform
        orig = conform(orig, interpol, conform_vox_size=_conform_vox_size)

    # Collect header and affine information
    header_info = orig.header
    affine_info = orig.affine
    orig_data = np.asanyarray(orig.dataobj)

    return header_info, affine_info, orig_data


def load_image(
        file: str,
        name: str = "image",
        **kwargs
) -> Tuple[nib.analyze.SpatialImage, np.ndarray]:
    """Load file 'file' with nibabel, including all data.

    Parameters
    ----------
    file : str
        path to the file to load.
    name : str
        name of the file (optional), only effects error messages. (Default value = "image")
    **kwargs :


    Returns
    -------
    Tuple[nib.analyze.SpatialImage, np.ndarray]
        The nibabel image object and a numpy array of the data.

    Raises
    ------
    IOError
        Failed loading the file
        nibabel releases the GIL, so the following is a parallel example.
        {
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor() as pool:
        future1 = pool.submit(load_image, filename1)
        future2 = pool.submit(load_image, filename2)
        image, data = future1.result()
        image2, data2 = future2.result()
        }

    """
    try:
        img = nib.load(file, **kwargs)
    except (IOError, FileNotFoundError) as e:
        raise IOError(
            f"Failed loading the {name} '{file}' with error: {e.args[0]}"
        ) from e
    data = np.asarray(img.dataobj)
    return img, data


def load_maybe_conform(
        file: str,
        alt_file: str,
        vox_size: VoxSizeOption = "min"
) -> Tuple[str, nib.analyze.SpatialImage, np.ndarray]:
    """Load an image by file, check whether it is conformed to vox_size and conform to vox_size if it is not.

    Parameters
    ----------
    file : str
        path to the file to load.
    alt_file : str
        alternative file to interpolate from
    vox_size : VoxSizeOption
        Voxel Size (Default value = "min")

    Returns
    -------
    Tuple[str, nib.analyze.SpatialImage, np.ndarray]
        [MISSING]

    """
    from os.path import isfile

    _is_conform, img = False, None
    if isfile(file):
        # see if the file is 1mm
        img = nib.load(file)
        # is_conform only needs the header, not the data
        _is_conform = is_conform(img, conform_vox_size=vox_size, verbose=False)

    if _is_conform:
        # calling np.asarray here, forces the load of img.dataobj into memory
        # (which is parallel with other operations, if done here)
        data = np.asarray(img.dataobj)
        dst_file = file
    else:
        # the image is not conformed to 1mm, do this now.
        from nibabel.filebasedimages import FileBasedHeader as _Header

        fileext = list(
            filter(lambda ext: file.endswith("." + ext), SUPPORTED_OUTPUT_FILE_FORMATS)
        )
        if len(fileext) != 1:
            raise RuntimeError(
                f"Invalid file extension of conf_name: {file}, must be one of "
                f"{SUPPORTED_OUTPUT_FILE_FORMATS}."
            )
        file_no_fileext = file[: -len(fileext[0]) - 1]
        vox_suffix = "." + (
            "min" if vox_size == "min" else str(vox_size) + "mm"
        ).replace(".", "")
        if not file_no_fileext.endswith(vox_suffix):
            file_no_fileext += vox_suffix
        # if the orig file is neither absolute nor in the subject path, use the conformed file
        src_file = alt_file if isfile(alt_file) else file
        if not isfile(alt_file):
            LOGGER.warning(
                f"No valid alternative file (e.g. orig, here: {alt_file}) was given to interpolate from, so "
                f"we might lose quality due to multiple chained interpolations."
            )

        dst_file = file_no_fileext + "." + fileext[0]
        # conform to 1mm
        header, affine, data = load_and_conform_image(
            src_file, conform_min=False, logger=logging.getLogger(__name__ + ".conform")
        )

        # after conforming, save the conformed file
        save_image(header, affine, data, dst_file)
        img = nib.MGHImage(data, affine, header)
    return dst_file, img, data


# Save image routine
def save_image(
        header_info: _Header,
        affine_info: npt.NDArray,
        img_array: npt.NDArray,
        save_as: str,
        dtype: Optional[npt.DTypeLike] = None
) -> None:
    """Save an image (nibabel MGHImage), according to the desired output file format.
    Supported formats are defined in supported_output_file_formats. Saves predictions to save_as

    Parameters
    ----------
    header_info : _Header
        image header information
    affine_info : npt.NDArray
        image affine information
    img_array : npt.NDArray
        an array containing image data
    save_as : str
        name under which to save prediction; this determines output file format
    dtype : Optional[npt.DTypeLike]
        image array type; if provided, the image object is explicitly set to match this type
         (Default value = None)

    """

    assert any(
        save_as.endswith(file_ext) for file_ext in SUPPORTED_OUTPUT_FILE_FORMATS
    ), (
        "Output filename does not contain a supported file format ("
        + ", ".join(file_ext for file_ext in SUPPORTED_OUTPUT_FILE_FORMATS)
        + ")!"
    )

    mgh_img = None
    if save_as.endswith("mgz"):
        mgh_img = nib.MGHImage(img_array, affine_info, header_info)
    elif any(save_as.endswith(file_ext) for file_ext in ["nii", "nii.gz"]):
        mgh_img = nib.nifti1.Nifti1Pair(img_array, affine_info, header_info)

    if dtype is not None:
        mgh_img.set_data_dtype(dtype)

    if any(save_as.endswith(file_ext) for file_ext in ["mgz", "nii"]):
        nib.save(mgh_img, save_as)
    elif save_as.endswith("nii.gz"):
        # For correct outputs, nii.gz files should be saved using the nifti1 sub-module's save():
        nib.nifti1.save(mgh_img, save_as)


# Transformation for mapping
def transform_axial(
        vol: npt.NDArray,
        coronal2axial: bool = True
) -> np.ndarray:
    """Function to transform volume into Axial axis and back

    Parameters
    ----------
    vol : npt.NDArray
        image volume to transform
    coronal2axial : bool
        transform from coronal to axial = True (default),

    Returns
    -------
    np.ndarray
        Transformed image
    """

    if coronal2axial:
        return np.moveaxis(vol, [0, 1, 2], [1, 2, 0])
    else:
        return np.moveaxis(vol, [0, 1, 2], [2, 0, 1])


def transform_sagittal(
        vol: npt.NDArray,
        coronal2sagittal: bool = True
) -> np.ndarray:
    """Function to transform volume into Sagittal axis and back

    Parameters
    ----------
    vol : npt.NDArray
        image volume to transform
    coronal2sagittal : bool
        transform from coronal to sagittal = True (default),

    Returns
    -------
    np.ndarray:
        transformed image


    """

    if coronal2sagittal:
        return np.moveaxis(vol, [0, 1, 2], [2, 1, 0])
    else:
        return np.moveaxis(vol, [0, 1, 2], [2, 1, 0])


# Thick slice generator (for eval) and blank slices filter (for training)
def get_thick_slices(
        img_data: npt.NDArray,
        slice_thickness: int = 3
) -> np.ndarray:
    """Function to extract thick slices from the image
    (feed slice_thickness preceding and succeeding slices to network,
    label only middle one)

    Parameters
    ----------
    img_data : npt.NDArray
        3D MRI image read in with nibabel
    slice_thickness : int
        number of slices to stack on top and below slice of interest (default=3)

    Returns
    -------
        np.ndarray
            image data with the thick slices of the n-th axis appended into the n+1-th axis.
    """
    img_data_pad = np.pad(
        img_data, ((0, 0), (0, 0), (slice_thickness, slice_thickness)), mode="edge"
    )
    from numpy.lib.stride_tricks import sliding_window_view

    # sliding_window_view will automatically create thick slices through a sliding window, but as this in only a view,
    # less memory copies are required
    return sliding_window_view(img_data_pad, 2 * slice_thickness + 1, axis=2)


def filter_blank_slices_thick(
        img_vol: npt.NDArray,
        label_vol: npt.NDArray,
        weight_vol: npt.NDArray,
        threshold: int = 50
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Function to filter blank slices from the volume using the label volume

    Parameters
    ----------
    img_vol : npt.NDArray
        orig image volume
    label_vol : npt.NDArray
        label images (ground truth)
    weight_vol : npt.NDArray
        weight corresponding to labels
    threshold : int
        threshold for number of pixels needed to keep slice (below = dropped). (Default value = 50)

    Returns
    -------
    filtered img_vol : np.ndarray
        [MISSING]
    label_vol : np.ndarray
        [MISSING]
    weight_vol : np.ndarray
        [MISSING]
    """

    # Get indices of all slices with more than threshold labels/pixels
    select_slices = np.sum(label_vol, axis=(0, 1)) > threshold

    # Retain only slices with more than threshold labels/pixels
    img_vol = img_vol[:, :, select_slices, :]
    label_vol = label_vol[:, :, select_slices]
    weight_vol = weight_vol[:, :, select_slices]

    return img_vol, label_vol, weight_vol


# weight map generator
def create_weight_mask(
        mapped_aseg: npt.NDArray,
        max_weight: int = 5,
        max_edge_weight: int = 5,
        max_hires_weight: Optional[int] = None,
        ctx_thresh: int = 33,
        mean_filter: bool = False,
        cortex_mask: bool = True,
        gradient: bool = True
) -> np.ndarray:
    """Function to create weighted mask - with median frequency balancing and edge-weighting

    Parameters
    ----------
    mapped_aseg : np.ndarray
        segmentation to create weight mask from.
    max_weight : int
        maximal weight on median weights (cap at this value). (Default value = 5)
    max_edge_weight : int
        maximal weight on gradient weight (cap at this value). (Default value = 5)
    max_hires_weight : int
        maximal weight on hires weight (cap at this value). (Default value = None)
    ctx_thresh : int
        label value of cortex (above = cortical parcels). (Default value = 33)
    mean_filter : bool
        flag, set to add mean_filter mask (default = False).
    cortex_mask : bool
        flag, set to create cortex weight mask (default=True).
    gradient : bool
        flag, set to create gradient mask (default = True).

    Returns
    -------
    np.ndarray
        Weights

    """

    unique, counts = np.unique(mapped_aseg, return_counts=True)

    # Median Frequency Balancing
    class_wise_weights = np.median(counts) / counts
    class_wise_weights[class_wise_weights > max_weight] = max_weight
    (h, w, d) = mapped_aseg.shape

    weights_mask = np.reshape(class_wise_weights[mapped_aseg.ravel()], (h, w, d))

    # Gradient Weighting
    if gradient:
        (gx, gy, gz) = np.gradient(mapped_aseg)
        grad_weight = max_edge_weight * np.asarray(
            np.power(np.power(gx, 2) + np.power(gy, 2) + np.power(gz, 2), 0.5) > 0,
            dtype="float",
        )

        weights_mask += grad_weight

    if max_hires_weight is not None:
        # High-res Weighting
        print(
            "Adding hires weight mask deep sulci and WM with weight ", max_hires_weight
        )
        mask1 = deep_sulci_and_wm_strand_mask(
            mapped_aseg, structure=np.ones((3, 3, 3)), ctx_thresh=ctx_thresh
        )
        weights_mask += mask1 * max_hires_weight

        if cortex_mask:
            print("Adding cortex mask with weight ", max_hires_weight)
            mask2 = cortex_border_mask(
                mapped_aseg, structure=np.ones((3, 3, 3)), ctx_thresh=ctx_thresh
            )
            weights_mask += mask2 * (max_hires_weight) // 2

    if mean_filter:
        weights_mask = uniform_filter(weights_mask, size=3)

    return weights_mask


def cortex_border_mask(
        label: npt.NDArray,
        structure: npt.NDArray,
        ctx_thresh: int = 33
) -> np.ndarray:
    """Function to erode the cortex of a given mri image to create
    the inner gray matter mask (outer most cortex voxels)

    Parameters
    ----------
    label : npt.NDArray
        ground truth labels.
    structure : npt.NDArray
        structuring element to erode with
    ctx_thresh : int
        label value of cortex (above = cortical parcels). Defaults to 33

    Returns
    -------
    np.ndarray
        inner grey matter layer


    """

    # create aseg brainmask, erode it and subtract from itself
    bm = np.clip(label, a_max=1, a_min=0)
    eroded = binary_erosion(bm, structure=structure)
    diff_im = np.logical_xor(eroded, bm)

    # only keep values associated with the cortex
    diff_im[(label <= ctx_thresh)] = 0  # > 33 (>19) = > 1002 in FS space (full (sag)),
    print("Remaining voxels cortex border: ", np.unique(diff_im, return_counts=True))
    return diff_im


def deep_sulci_and_wm_strand_mask(
        volume: npt.NDArray,
        structure: npt.NDArray,
        iteration: int = 1,
        ctx_thresh: int = 33
) -> np.ndarray:
    """Function to get a binary mask of deep sulci and small white matter strands
     by using binary closing (erosion and dilation)

    Parameters
    ----------
    volume : npt.NDArray
        loaded image (aseg, label space)
    structure : npt.NDArray
        structuring element (e.g. np.ones((3, 3, 3)))
    iteration : int
        number of times mask should be dilated + eroded. Defaults to 1
    ctx_thresh : int
        label value of cortex (above = cortical parcels). Defaults to 33

    Returns
    -------
    np.ndarray
        sulcus + wm mask

    """

    # Binarize label image (cortex = 1, everything else = 0)
    empty_im = np.zeros(shape=volume.shape)
    empty_im[volume > ctx_thresh] = 1  # > 33 (>19) = >1002 in FS LUT (full (sag))

    # Erode the image
    eroded = binary_closing(empty_im, iterations=iteration, structure=structure)

    # Get difference between eroded and original image
    diff_image = np.logical_xor(empty_im, eroded)
    print(
        "Remaining voxels sulci/wm strand: ", np.unique(diff_image, return_counts=True)
    )
    return diff_image


# Label mapping functions (to aparc (eval) and to label (train))
def read_classes_from_lut(lut_file: str) -> pd.DataFrame:
    """Function to read in FreeSurfer-like LUT table

    Parameters
    ----------
    lut_file : str
        path and name of FreeSurfer-style LUT file with classes of interest
        Example entry:
        ID LabelName  R   G   B   A
        0   Unknown   0   0   0   0
        1   Left-Cerebral-Exterior 70  130 180 0

    Returns
    -------
    pd.Dataframe
        DataFrame with ids present, name of ids, color for plotting


    """
    # Read in file
    separator = {"tsv": "\t", "csv": ",", "txt": " "}
    return pd.read_csv(lut_file, sep=separator[lut_file[-3:]])


def map_label2aparc_aseg(
        mapped_aseg: torch.Tensor,
        labels: Union[torch.Tensor, npt.NDArray]
) -> torch.Tensor:
    """Function to perform look-up table mapping from sequential label space to LUT space

    Parameters
    ----------
    mapped_aseg : torch.Tensor
        label space segmentation (aparc.DKTatlas + aseg)
    labels : Union[torch.Tensor, npt.NDArray]
        list of labels defining LUT space

    Returns
    -------
    torch.Tensor
        labels in LUT space

    """

    if isinstance(labels, np.ndarray):
        labels = torch.from_numpy(labels)
    labels = labels.to(mapped_aseg.device)
    return labels[mapped_aseg]


def clean_cortex_labels(aparc: npt.NDArray) -> np.ndarray:
    """Function to clean up aparc segmentations:
        Map undetermined and optic chiasma to BKG
        Map Hypointensity classes to one
        Vessel to WM
        5th Ventricle to CSF
        Remaining cortical labels to BKG

    Parameters
    ----------
    aparc : npt.NDArray
        aparc segmentations

    Returns
    -------
    np.ndarray
        cleaned aparc

    """

    aparc[aparc == 80] = 77  # Hypointensities Class
    aparc[aparc == 85] = 0  # Optic Chiasma to BKG
    aparc[aparc == 62] = 41  # Right Vessel to Right WM
    aparc[aparc == 30] = 2  # Left Vessel to Left WM
    aparc[aparc == 72] = 24  # 5th Ventricle to CSF
    aparc[aparc == 29] = 0  # left-undetermined to 0
    aparc[aparc == 61] = 0  # right-undetermined to 0

    aparc[aparc == 3] = 0  # Map Remaining Cortical labels to background
    aparc[aparc == 42] = 0
    return aparc


def fill_unknown_labels_per_hemi(
        gt: npt.NDArray,
        unknown_label: int,
        cortex_stop: int
) -> np.ndarray:
    """Function to replace label 1000 (lh unknown) and 2000 (rh unknown) with closest class for each voxel.

    Parameters
    ----------
    gt : npt.NDArray
        ground truth segmentation with class unknown
    unknown_label : int
        class label for unknown (lh: 1000, rh: 2000)
    cortex_stop : int
        class label at which cortical labels of this hemi stop (lh: 2000, rh: 3000)

    Returns
    -------
        np.ndarray
            ground truth segmentation with all classes

    """

    # Define shape of image and dilation element
    h, w, d = gt.shape
    struct1 = generate_binary_structure(3, 2)

    # Get indices of unknown labels, dilate them to get closest surrounding parcels
    unknown = gt == unknown_label
    unknown = morphology.binary_dilation(unknown, struct1) ^ unknown
    list_parcels = np.unique(gt[unknown])

    # Mask all subcortical structures (fill unknown with closest cortical parcels only)
    mask = (list_parcels > unknown_label) & (list_parcels < cortex_stop)
    list_parcels = list_parcels[mask]

    # For each closest parcel, blur label with gaussian filter (spread), append resulting blurred images
    blur_vals = np.ndarray((h, w, d, 0), dtype=float)
    for idx in range(len(list_parcels)):
        aseg_blur = filters.gaussian_filter(
            1000 * np.asarray(gt == list_parcels[idx], dtype=float), sigma=5
        )
        blur_vals = np.append(blur_vals, np.expand_dims(aseg_blur, axis=3), axis=3)

    # Get for each position parcel with maximum value after blurring (= closest parcel)
    unknown = np.argmax(blur_vals, axis=3)
    unknown = np.reshape(list_parcels[unknown.ravel()], (h, w, d))

    # Assign the determined closest parcel to the unknown class (case-by-case basis)
    mask = gt == unknown_label
    gt[mask] = unknown[mask]

    return gt


def fuse_cortex_labels(aparc: npt.NDArray) -> np.ndarray:
    """Fuse cortical parcels on left/right hemisphere (reduce aparc classes)

    Parameters
    ----------
    aparc : npt.NDArray
        anatomical segmentation with cortical parcels

    Returns
    -------
    np.ndarray
        anatomical segmentation with reduced number of cortical parcels

    """

    aparc_temp = aparc.copy()

    # Map undetermined classes
    aparc = clean_cortex_labels(aparc)

    # Fill label unknown
    if np.any(aparc == 1000):
        aparc = fill_unknown_labels_per_hemi(aparc, 1000, 2000)
    if np.any(aparc == 2000):
        aparc = fill_unknown_labels_per_hemi(aparc, 2000, 3000)

    # De-lateralize parcels
    cortical_label_mask = (aparc >= 2000) & (aparc <= 2999)
    aparc[cortical_label_mask] = aparc[cortical_label_mask] - 1000

    # Re-lateralize Cortical parcels in close proximity
    aparc[aparc_temp == 2014] = 2014
    aparc[aparc_temp == 2028] = 2028
    aparc[aparc_temp == 2012] = 2012
    aparc[aparc_temp == 2016] = 2016
    aparc[aparc_temp == 2002] = 2002
    aparc[aparc_temp == 2023] = 2023
    aparc[aparc_temp == 2017] = 2017
    aparc[aparc_temp == 2024] = 2024
    aparc[aparc_temp == 2010] = 2010
    aparc[aparc_temp == 2013] = 2013
    aparc[aparc_temp == 2025] = 2025
    aparc[aparc_temp == 2022] = 2022
    aparc[aparc_temp == 2021] = 2021
    aparc[aparc_temp == 2005] = 2005

    return aparc


def split_cortex_labels(aparc: npt.NDArray) -> np.ndarray:
    """Splot cortex labels to completely de-lateralize structures

    Parameters
    ----------
    aparc : npt.NDArray
        anatomical segmentation and parcellation from network

    Returns
    -------
    np.ndarray
        re-lateralized aparc

    """

    # Post processing - Splitting classes
    # Quick Fix for 2026 vs 1026; 2029 vs. 1029; 2025 vs. 1025
    rh_wm = get_largest_cc(aparc == 41)
    lh_wm = get_largest_cc(aparc == 2)
    rh_wm = regionprops(label(rh_wm, background=0))
    lh_wm = regionprops(label(lh_wm, background=0))
    centroid_rh = np.asarray(rh_wm[0].centroid)
    centroid_lh = np.asarray(lh_wm[0].centroid)

    labels_list = np.array(
        [
            1003,
            1006,
            1007,
            1008,
            1009,
            1011,
            1015,
            1018,
            1019,
            1020,
            1025,
            1026,
            1027,
            1028,
            1029,
            1030,
            1031,
            1034,
            1035,
        ]
    )

    for label_current in labels_list:

        label_img = label(aparc == label_current, connectivity=3, background=0)

        for region in regionprops(label_img):

            if region.label != 0:  # To avoid background

                if np.linalg.norm(
                    np.asarray(region.centroid) - centroid_rh
                ) < np.linalg.norm(np.asarray(region.centroid) - centroid_lh):
                    mask = label_img == region.label
                    aparc[mask] = label_current + 1000

    # Quick Fixes for overlapping classes
    aseg_lh = filters.gaussian_filter(
        1000 * np.asarray(aparc == 2, dtype=float), sigma=3
    )
    aseg_rh = filters.gaussian_filter(
        1000 * np.asarray(aparc == 41, dtype=float), sigma=3
    )

    lh_rh_split = np.argmax(
        np.concatenate(
            (np.expand_dims(aseg_lh, axis=3), np.expand_dims(aseg_rh, axis=3)), axis=3
        ),
        axis=3,
    )

    # Problematic classes: 1026, 1011, 1029, 1019
    for prob_class_lh in [1011, 1019, 1026, 1029]:
        prob_class_rh = prob_class_lh + 1000
        mask_prob_class = (aparc == prob_class_lh) | (aparc == prob_class_rh)
        mask_lh = np.logical_and(mask_prob_class, lh_rh_split == 0)
        mask_rh = np.logical_and(mask_prob_class, lh_rh_split == 1)

        aparc[mask_lh] = prob_class_lh
        aparc[mask_rh] = prob_class_rh

    return aparc


def unify_lateralized_labels(
        lut: Union[str, pd.DataFrame],
        combi: Tuple[str, str] = ("Left-", "Right-")
) -> Mapping:
    """Function to generate lookup dictionary of left-right labels

    Parameters
    ----------
    lut : Union[str, pd.DataFrame]
        either lut-file string to load or pandas dataframe
        Example entry:
        ID LabelName  R   G   B   A
        0   Unknown   0   0   0   0
        1   Left-Cerebral-Exterior 70  130 180 0
    combi : Tuple[str, str]
        Prefix or labelnames to combine. Default: Left- and Right-

    Returns
    -------
    Mapping
        dictionary mapping between left and right hemispheres

    """

    if isinstance(lut, str):
        lut = read_classes_from_lut(lut)
    left = lut[["ID", "LabelName"]][lut["LabelName"].str.startswith(combi[0])]
    right = lut[["ID", "LabelName"]][lut["LabelName"].str.startswith(combi[1])]
    left["LabelName"] = left["LabelName"].str.removeprefix(combi[0])
    right["LabelName"] = right["LabelName"].str.removeprefix(combi[1])
    mapp = left.merge(right, on="LabelName")
    return pd.Series(mapp.ID_y.values, index=mapp.ID_x).to_dict()


def get_labels_from_lut(
        lut: Union[str, pd.DataFrame],
        label_extract: Tuple[str, str] = ("Left-", "ctx-rh")
) -> Tuple[np.ndarray, np.ndarray]:
    """Function to extract labels from the lookup tables

    Parameters
    ----------
    lut : Union[str, pd.DataFrame]
        FreeSurfer like LookUp Table (either path to it
        or already loaded as pandas DataFrame.
        Example entry:
        ID LabelName  R   G   B   A
        0   Unknown   0   0   0   0
        1   Left-Cerebral-Exterior 70  130 180 0
    label_extract : Tuple[str, str]
        suffix of label names to mask for sagittal labels
        Default: "Left-" and "ctx-rh"

    Returns
    -------
    np.ndarray
        full label list,
    np.ndarray
        sagittal label list

    """

    if isinstance(lut, str):
        lut = read_classes_from_lut(lut)
    mask = lut["LabelName"].str.startswith(label_extract)
    return lut["ID"].values, lut["ID"][~mask].values


def map_aparc_aseg2label(
        aseg: npt.NDArray,
        labels: npt.NDArray,
        labels_sag: npt.NDArray,
        sagittal_lut_dict: Mapping,
        aseg_nocc: Optional[npt.NDArray] = None,
        processing:  str = "aparc"
) ->  Tuple[np.ndarray, np.ndarray]:
    """Function to perform look-up table mapping of aparc.DKTatlas+aseg.mgz data to label space

    Parameters
    ----------
    aseg : npt.NDArray
        ground truth aparc+aseg
    labels : npt.NDArray
        labels to use (extracted from LUT with get_labels_from_lut)
    labels_sag : npt.NDArray
        sagittal labels to use (extracted from LUT with
        get_labels_from_lut)
    sagittal_lut_dict : Mapping
        left-right label mapping (can be extracted with
        unify_lateralized_labels from LUT)
    aseg_nocc : Optional[npt.NDArray]
        ground truth aseg without corpus callosum segmentation (Default value = None)
    processing : str
        should be set to "aparc" or "aseg" for additional mappings (hard-coded) (Default value = "aparc")

    Returns
    -------
    np.ndarray
        mapped aseg for coronal and axial,
    np.ndarray
        mapped aseg for sagital

    """

    # If corpus callosum is not removed yet, do it now
    if aseg_nocc is not None:
        cc_mask = (aseg >= 251) & (aseg <= 255)
        aseg[cc_mask] = aseg_nocc[cc_mask]

    if processing == "aparc":
        LOGGER.info("APARC PROCESSING")
        aseg = fuse_cortex_labels(aseg)

    elif processing == "aseg":
        LOGGER.info("ASEG PROCESSING")
        aseg[aseg == 1000] = 3  # Map unknown to cortex
        aseg[aseg == 2000] = 42
        aseg[aseg == 80] = 77  # Hypointensities Class
        aseg[aseg == 85] = 0  # Optic Chiasma to BKG
        aseg[aseg == 62] = 41  # Right Vessel to Right WM
        aseg[aseg == 30] = 2  # Left Vessel to Left WM
        aseg[aseg == 72] = 24  # 5th Ventricle to CSF

        assert not np.any(
            251 <= aseg
        ), "Error: CC classes (251-255) still exist in aseg {}".format(np.unique(aseg))
        assert np.any(aseg == 3) and np.any(
            aseg == 42
        ), "Error: no cortical marker detected {}".format(np.unique(aseg))

    assert set(labels).issuperset(
        np.unique(aseg)
    ), "Error: segmentation image contains classes not listed in the labels: \n{}\n{}".format(
        np.unique(aseg), labels
    )

    h, w, d = aseg.shape
    lut_aseg = np.zeros(max(labels) + 1, dtype="int")
    for idx, value in enumerate(labels):
        lut_aseg[value] = idx

    # Remap Label Classes - Perform LUT Mapping - Coronal, Axial
    mapped_aseg = lut_aseg.ravel()[aseg.ravel()]
    mapped_aseg = mapped_aseg.reshape((h, w, d))

    if processing == "aparc":
        cortical_label_mask = (aseg >= 2000) & (aseg <= 2999)
        aseg[cortical_label_mask] = aseg[cortical_label_mask] - 1000

    # For sagittal, all Left hemispheres will be mapped to right, ctx the otherway round
    # If you use your own LUT, make sure all per-hemi labels have the corresponding prefix
    # Map Sagittal Labels
    for left, right in sagittal_lut_dict.items():
        aseg[aseg == left] = right

    h, w, d = aseg.shape
    lut_aseg = np.zeros(max(labels_sag) + 1, dtype="int")
    for idx, value in enumerate(labels_sag):
        lut_aseg[value] = idx

    # Remap Label Classes - Perform LUT Mapping - Coronal, Axial
    mapped_aseg_sag = lut_aseg.ravel()[aseg.ravel()]
    mapped_aseg_sag = mapped_aseg_sag.reshape((h, w, d))

    return mapped_aseg, mapped_aseg_sag


def sagittal_coronal_remap_lookup(x: int) -> int:
    """Dictionary mapping to convert left labels to corresponding right labels for aseg

    Parameters
    ----------
    x : int
        label to look up

    Returns
    -------
    np.ndarray
        mapped label

    """

    return {
        2: 41,
        3: 42,
        4: 43,
        5: 44,
        7: 46,
        8: 47,
        10: 49,
        11: 50,
        12: 51,
        13: 52,
        17: 53,
        18: 54,
        26: 58,
        28: 60,
        31: 63,
    }[x]


def infer_mapping_from_lut(
        num_classes_full: int,
        lut: Union[str, pd.DataFrame]
) -> np.ndarray:
    """[MISSING]

    Parameters
    ----------
    num_classes_full : int
        number of classes
    lut : Union[str, pd.DataFrame]
        look-up table listing class labels

    Returns
    -------
    np.ndarray
        list of indexes for

    """

    labels, labels_sag = unify_lateralized_labels(lut)
    idx_list = np.ndarray(shape=(num_classes_full,), dtype=np.int16)
    for idx in range(len(labels)):
        idx_in_sag = np.where(labels_sag == labels[idx])[0]
        if idx_in_sag.size == 0:  # Empty not subcortical
            idx_in_sag = np.where(labels_sag == (labels[idx] - 1000))[0]

        if idx_in_sag.size == 0:
            current_label_sag = sagittal_coronal_remap_lookup(labels[idx])
            idx_in_sag = np.where(labels_sag == current_label_sag)[0]

        idx_list[idx] = idx_in_sag
    return idx_list


def map_prediction_sagittal2full(
        prediction_sag: npt.NDArray,
        num_classes: int = 51,
        lut: Optional[str] = None
) -> np.ndarray:
    """Function to remap the prediction on the sagittal network to full label space used by coronal and axial networks
    Creates full aparc.DKTatlas+aseg.mgz.

    Parameters
    ----------
    prediction_sag : npt.NDArray
        sagittal prediction (labels)
    num_classes : int
        number of SAGITTAL classes (96 for full classes, 51 for hemi split, 21 for aseg) (Default value = 51)
    lut : Optional[str]
        look-up table listing class labels (Default value = None)

    Returns
    -------
    np.ndarray
        Remapped prediction

    """

    if num_classes == 96:
        idx_list = np.asarray(
            [
                0,
                5,
                6,
                7,
                8,
                9,
                10,
                11,
                12,
                13,
                1,
                2,
                3,
                14,
                15,
                4,
                16,
                17,
                18,
                5,
                6,
                7,
                8,
                9,
                10,
                11,
                12,
                13,
                14,
                15,
                16,
                17,
                18,
                19,
                20,
                21,
                22,
                23,
                24,
                25,
                26,
                27,
                28,
                29,
                30,
                31,
                32,
                33,
                34,
                35,
                36,
                37,
                38,
                39,
                40,
                41,
                42,
                43,
                44,
                45,
                46,
                47,
                48,
                49,
                50,
                20,
                21,
                22,
                23,
                24,
                25,
                26,
                27,
                28,
                29,
                30,
                31,
                32,
                33,
                34,
                35,
                36,
                37,
                38,
                39,
                40,
                41,
                42,
                43,
                44,
                45,
                46,
                47,
                48,
                49,
                50,
            ],
            dtype=np.int16,
        )

    elif num_classes == 51:
        idx_list = np.asarray(
            [
                0,
                5,
                6,
                7,
                8,
                9,
                10,
                11,
                12,
                13,
                1,
                2,
                3,
                14,
                15,
                4,
                16,
                17,
                18,
                5,
                6,
                7,
                8,
                9,
                10,
                11,
                12,
                13,
                14,
                15,
                16,
                17,
                18,
                19,
                20,
                21,
                22,
                23,
                24,
                25,
                26,
                27,
                28,
                29,
                30,
                31,
                32,
                33,
                34,
                35,
                36,
                37,
                38,
                39,
                40,
                41,
                42,
                43,
                44,
                45,
                46,
                47,
                48,
                49,
                50,
                20,
                22,
                27,
                29,
                30,
                31,
                33,
                34,
                38,
                39,
                40,
                41,
                42,
                45,
            ],
            dtype=np.int16,
        )

    elif num_classes == 21:
        idx_list = np.asarray(
            [
                0,
                5,
                6,
                7,
                8,
                9,
                10,
                11,
                12,
                13,
                14,
                1,
                2,
                3,
                15,
                16,
                4,
                17,
                18,
                19,
                5,
                6,
                7,
                8,
                9,
                10,
                11,
                12,
                13,
                14,
                15,
                16,
                17,
                18,
                19,
                20,
            ],
            dtype=np.int16,
        )

    else:
        assert lut is not None, "lut is not defined!"
        idx_list = infer_mapping_from_lut(num_classes, lut)
    prediction_full = prediction_sag[:, idx_list, :, :]
    return prediction_full


# Clean up and class separation
def bbox_3d(
        img: npt.NDArray
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """[MISSING]
    Function to extract the three-dimensional bounding box coordinates.

    Parameters
    ----------
    img : npt.NDArray
        mri image

    Returns
    -------
    np.ndarray
        rmin
    np.ndarray
        rmax
    np.ndarray
        cmin
    np.ndarray
        cmax
    np.ndarray
        zmin
    np.ndarray
        zmax

    """

    r = np.any(img, axis=(1, 2))
    c = np.any(img, axis=(0, 2))
    z = np.any(img, axis=(0, 1))

    rmin, rmax = np.where(r)[0][[0, -1]]
    cmin, cmax = np.where(c)[0][[0, -1]]
    zmin, zmax = np.where(z)[0][[0, -1]]

    return rmin, rmax, cmin, cmax, zmin, zmax


def get_largest_cc(segmentation: npt.NDArray) -> np.ndarray:
    """Function to find largest connected component of segmentation.

    Parameters
    ----------
    segmentation : npt.NDArray
        segmentation

    Returns
    -------
    np.ndarray
        largest connected component of segmentation (binary mask)


    """

    labels = label(segmentation, connectivity=3, background=0)

    bincount = np.bincount(labels.flat)
    background = np.argmax(bincount)
    bincount[background] = -1

    largest_cc = labels == np.argmax(bincount)

    return largest_cc
