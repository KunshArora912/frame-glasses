"""Build a signed Frame DFU zip from build/application.hex, mirroring the
repo Makefile's `nrfutil pkg generate --hw-version 52 --application-version 0
--sd-req 0x0123 --key-file source/bootloader/dfu_private_key.pem`.
Usage: make_dfu.py <repo> <out.zip>
"""
import sys, os
from nordicsemi.dfu.package import Package
from nordicsemi.dfu.signing import Signing

repo, out = sys.argv[1], sys.argv[2]
signer = Signing()
signer.load_key(os.path.join(repo, 'source', 'bootloader', 'dfu_private_key.pem'))
pkg = Package(debug_mode=False, hw_version=52, app_version=0, bl_version=0xFFFFFFFF,
              sd_req=[0x0123], sd_id=[0x0123],
              app_fw=os.path.join(repo, 'build', 'application.hex'),
              signer=signer)
pkg.generate_package(out, preserve_work_dir=False)
print('wrote', out, os.path.getsize(out))
