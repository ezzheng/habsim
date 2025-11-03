from PIL import Image
import numpy as np

Image.MAX_IMAGE_PIXELS = 1_000_000_000  # raise the limit above the TIFF size

def tif_to_npy(tif_path, npy_path):
    with Image.open(tif_path) as img:
        img_half = img.resize((img.width // 2, img.height // 2), resample=Image.BILINEAR)
        img_array = np.array(img_half, dtype=np.int16)
    np.save(npy_path, img_array, allow_pickle=False)

# Mean - 30 arc seconds
tif_to_npy('data/gmted_tif/mn30_grd.tif', 'data/worldelev.npy')