# MF800H GC9107 hardware prerequisite

Reference **MSM8916/512 MB MF800H**, **kernel 6.12.1-msm8916** only. Not a generic
LCD installer. Requires the kernel's existing `panel-mipi-dbi` driver.

| Function | Wiring |
| --- | --- |
| SPI5 | GPIO16/17/19, 16 MHz |
| CS | GPIO18, active low |
| Reset / DC | GPIO11 / GPIO30 |
| Panel / logic supplies | L17 2.85 V / L6 1.8 V |
| Backlight | PM8916 MPP4, 10 mA sink |
| Panel | GC9107, 128 × 128, RGB565 transfers / XRGB8888 framebuffer |

`himi,mf800h-gc9107.bin` is the 134-byte panel initialization sequence, converted
from the stock command table. `mf800h-lcd.dts` includes the self-contained
`mf800h.dts` and `upstream-mf800.dts`. The base is reconstructed from the same
public board profile and battery LUT arrays used by the flasher, never a private
backup. The inherited OCV curve remains unvalidated. The charger node stays off.

Rebuild from this directory into a new output file (requires `dtc`):

```sh
dtc -q -I dts -O dtb -o /path/to/NEW-mf800h-lcd.dtb mf800h-lcd.dts
```

Expected supplied DTB SHA-256:
`b34a47947201c9eaf84966dd8a4381d9a1ca02b0c77c990a78ed85d03da9ac58`.
Panel firmware SHA-256:
`bcc3e4ad2a9069841682770a1783d34aa7e726e7e82a11ce0546c71b113e4908`.
See `../SHA256SUMS` for all source/artifact hashes.

## Fresh headless installation only

First confirm the board, `uname -r`, `findmnt /boot` and working USB SSH. Save
the original extlinux configuration and headless DTB to **new private backup
paths**; do not overwrite an existing recovery copy. Retain physical recovery
access and stable power. Copy the supplied LCD DTB to
`/boot/dtbs/qcom/msm8916-mf800h-lcd.dtb` and panel firmware to
`/lib/firmware/himi,mf800h-gc9107.bin`, mode 0644. Deliberately select the new
DTB in your own `/boot/extlinux/extlinux.conf`, review that boot entry, sync
and reboot only when safe. Do not automate this against an unknown boot layout.

After boot confirm `/dev/fb0`, 128 × 128, 32 bpp and the `mf800h:lcd` backlight,
then follow the standalone daemon installation in `../README.md`. Never run
the old and new framebuffer renderer simultaneously.

For headless rollback: disable both framebuffer services, restore **your own**
saved pre-LCD extlinux configuration selecting the untouched headless DTB,
sync and explicitly reboot. No private boot configuration is bundled.

This work does not enable charging, change thermal limits or implement an
80% charge limit. A battery icon, voltage increase or BMS `Discharging` label
does not establish measured charge current. Public redistribution of upstream
and stock-derived hardware data still requires owner/license review.
