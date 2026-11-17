#!/usr/bin/env bash
# Issue server leaf certs for every host the emulator/broker will answer for.
# Run once after generating the offline CA; outputs to pki/leaves/.
set -euo pipefail
cd "$(dirname "$0")"

HOSTS=(
  us-01.mqtt.narwaltech.com eu-01.mqtt.narwaltech.com cn-mqtt.narwaltech.com
  us-mqtt.narwaltech.com eu-mqtt.narwaltech.com
  us-open.narwaltech.com eu-open.narwaltech.com cn-open.narwaltech.com
  iot-auth-global.narwaltech.com
  us.device.narwaltech.com eu.device.narwaltech.com cn.device.narwaltech.com
  aus.device.narwaltech.com il.device.narwaltech.com
  us-app.narwaltech.com eu-app.narwaltech.com il-app.narwaltech.com
  cn-app.narwaltech.com aus-app.narwaltech.com de-app.narwaltech.com
  us-app-sweeper.narwaltech.com eu-app-sweeper.narwaltech.com
  il-app-sweeper.narwaltech.com aus-app-sweeper.narwaltech.com
  aus-sweeper.narwaltech.com tcburyrobot.narwaltech.com
  us-open-data.narwaltech.com gallery-mobile.narwaltech.com
)

mkdir -p leaves
for h in "${HOSTS[@]}"; do
  [ -f "leaves/$h.pem" ] && continue
  openssl req -new -newkey rsa:2048 -nodes -keyout "leaves/$h.key" \
    -out "/tmp/$h.csr" -subj "/CN=$h" 2>/dev/null
  openssl x509 -req -in "/tmp/$h.csr" -CA offline-ca.crt -CAkey offline-ca.key \
    -CAcreateserial -out "leaves/$h.pem" -days 3650 -sha256 \
    -extfile <(printf 'subjectAltName=DNS:%s\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n' "$h") 2>/dev/null
  echo "issued leaves/$h.pem"
done
echo "done."
