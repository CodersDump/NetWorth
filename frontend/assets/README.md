# Static assets

Put your exported UPI QR code image here as `upi-qr.png` (or update the
`UPI_QR_IMAGE` path in `frontend/index.html` if you name it differently).

How to export it:
1. Open GPay / PhonePe / Paytm on your phone
2. Go to your profile / "Show QR code"
3. Save or share the image, get it onto your computer
4. Save it as `frontend/assets/upi-qr.png` in this repo, commit, push

No code changes needed after that - the Finance tab's payment card checks
this path first before falling back to a generated QR.
