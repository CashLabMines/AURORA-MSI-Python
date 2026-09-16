# boot.py
import usb_cdc

# Keep the REPL console available, and also enable a dedicated data port.
usb_cdc.enable(console=True, data=True)
