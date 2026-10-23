#!/usr/bin/env bash
# ==============================================================================
# Faristol - Instal·lador per a Linux
# ==============================================================================
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DEST="$HOME/.local/bin"
SYSTEMD_DEST="$HOME/.config/systemd/user"

echo "📦 Instal·lant Faristol..."

# 1. Comprovar dependències bàsiques
echo "🔍 Comprovant dependències..."
for cmd in python3 pip; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "⚠️  Avís: L'ordre '$cmd' no s'ha trobat al sistema."
    fi
done

# 2. Instal·lar l'executable al PATH d'usuari
mkdir -p "$BIN_DEST"
ln -sf "$DIR/bin/faristol" "$BIN_DEST/faristol"
chmod +x "$DIR/bin/faristol"
echo "  ✓ $BIN_DEST/faristol enllaçat"

# 3. Instal·lar i activar el servei d'usuari a systemd
mkdir -p "$SYSTEMD_DEST"
cp -f "$DIR/systemd/faristol.service" "$SYSTEMD_DEST/faristol.service"

systemctl --user daemon-reload
systemctl --user enable --now faristol.service

echo ""
echo "🎉 Faristol s'ha instal·lat i activat correctament amb systemd!"
echo "   (El servei s'iniciarà automàticament a cada inici de sessió)"
echo ""
echo "Estat del servei:"
systemctl --user status faristol.service --no-pager | head -n 8
echo ""
echo "Faristol està llest a: http://localhost:8080 (o via Tailscale: http://<IP-Tailscale>:8080)"
