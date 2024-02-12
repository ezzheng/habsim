from PIL import Image
import numpy as np

Image.MAX_IMAGE_PIXELS = 138240000

def tif_to_npy(tif_path, npy_path):
    with Image.open(tif_path) as img:
        img_array = np.array(img)
    np.save(npy_path, img_array)

# Mean - 30 arc seconds
tif_to_npy('./30N150W_20101117_gmted_mea300.tif', './gefs/worldelev.npy')