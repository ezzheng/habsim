HABSIM data directory

Place the required runtime datasets in this folder before uploading them to your Supabase bucket `habsim`:

Required files (bucket root):
- whichgefs
  - Text file containing a single GEFS run timestamp, e.g. `2025010106`.
  - Must exactly match the prefix of the wind files below.
- worldelev.npy
  - Global elevation array in NumPy format.
- <whichgefs>_01.npz, <whichgefs>_02.npz
  - GEFS wind datasets for at least two ensemble members (add `_03`…`_20` later).

Upload to Supabase Storage (bucket: `habsim`, object names at bucket root):
- whichgefs
- worldelev.npy
- 2025010106_01.npz, 2025010106_02.npz (and more as available)

More details documented here: https://docs.google.com/document/d/1um6OudR0FloEQ6i27HyFv45fZUg4RP2RIcs_hOXY1W0/edit?tab=t.0
