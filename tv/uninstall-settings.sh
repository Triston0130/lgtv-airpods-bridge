#!/bin/sh
# Removes the AirPods choices from Settings and Quick Settings: unmounts the patched copies and deletes them and the
# startup hook. Quick Settings returns to stock after the next TV restart if the system UI still holds the patch.
A=/usr/palm/applications/com.palm.app.settings
for target in "$A/main.js" "$A/appinfo.json" "$A" /usr/lib/qt5/qml/QuickSettings; do
  while grep -q " $target " /proc/mounts; do umount "$target" || break; done
done
rm -f /var/lib/webosbrew/init.d/69-airpods-settings
rm -rf /var/lib/webosbrew/airpods-bridge/settings
luna-send -n 1 luna://com.webos.applicationManager/closeByAppId '{"id":"com.palm.app.settings"}' >/dev/null 2>&1
echo "Stock Settings restored."
