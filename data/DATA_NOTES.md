HABSIM data directory

Place the required runtime datasets in this folder before uploading them to Supabase bucket `habsim`:

Required files (bucket root):
- whichgefs
  - Text file containing a single GEFS run timestamp, e.g. `2025010106`.
  - Must exactly match the prefix of the wind files below.
- worldelev.npy
  - Global elevation array in NumPy format.
- <whichgefs>_00.npz, <whichgefs>_01.npz, etc.
  - GEFS wind datasets for various ensemble members (_00 represents the control)

Upload to Supabase storage (bucket: `habsim`):
- whichgefs
- worldelev.npy
- 2025010106_01.npz, 2025010106_02.npz (demo)
