#!/usr/bin/env bash
#
# Levanta el registro de imágenes privado (Docker Registry) en esta máquina de Datos.
#
#   ./levantar-registry.sh            # levantar (o volver a levantar) el registry
#   ./levantar-registry.sh estado     # ¿está vivo? ¿qué imágenes y tags tiene?
#   ./levantar-registry.sh bajar      # bajar el contenedor sin borrar las capas
#   ./levantar-registry.sh limpiar    # garbage-collect de capas huérfanas
#
# El registry corre en la máquina de Datos y lo consultan los dev y el CD.
# Publica el puerto sólo en la IP de Tailscale: desde fuera del tailnet no existe.

set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONTENEDOR="${CONTENEDOR:-sdypp-registry}"
IMAGEN="${IMAGEN:-registry:2}"
PUERTO="${PUERTO:-5000}"

# Las capas de las imágenes van a un directorio del host.
DIR_REGISTRY="${DIR_REGISTRY:-$HOME/sdypp/registry}"

paso()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
info()  { printf '    %s\n' "$*"; }
aviso() { printf '\033[33m    ! %s\033[0m\n' "$*"; }
error() { printf '\033[31m!!! %s\033[0m\n' "$*" >&2; }

# --- Dónde escucha ---------------------------------------------------------

direccion_tailscale() {
    if [[ -n "${IP_TAILSCALE:-}" ]]; then
        echo "$IP_TAILSCALE"
        return
    fi
    local ip
    ip="$(tailscale ip -4 2>/dev/null | head -1 || true)"
    if [[ -z "$ip" ]]; then
        error "no pude averiguar la IP de Tailscale de esta máquina."
        info  "¿Está corriendo? Probá 'tailscale status'."
        info  "Si sabés cuál es, pasala a mano: IP_TAILSCALE=100.x.y.z $0"
        exit 1
    fi
    echo "$ip"
}

# --- Comandos --------------------------------------------------------------

levantar() {
    local ip
    ip="$(direccion_tailscale)"

    paso "LEVANTAR — el registry en $ip:$PUERTO"

    mkdir -p "$DIR_REGISTRY"
    docker rm -f "$CONTENEDOR" >/dev/null 2>&1 || true

    local duenio; duenio="$(id -u):$(id -g)"

    docker run -d \
        --name "$CONTENEDOR" \
        --restart unless-stopped \
        --user "$duenio" \
        -p "$ip:$PUERTO:5000" \
        -v "$DIR_REGISTRY:/var/lib/registry" \
        -e REGISTRY_STORAGE_DELETE_ENABLED=true \
        --health-cmd 'wget -qO- http://127.0.0.1:5000/v2/ >/dev/null || exit 1' \
        --health-interval 10s --health-timeout 3s --health-retries 3 \
        "$IMAGEN" >/dev/null

    esperar_vivo
    paso "LISTO"
    info "Publicado SÓLO en $ip:$PUERTO — desde fuera del tailnet no responde."
    echo
    info "Configuración requerida en /etc/docker/daemon.json de las casas y devs:"
    printf '\n    {"insecure-registries": ["%s:%s"]}\n\n' "$ip" "$PUERTO"
    aviso "Sin TLS/Auth porque el cifrado lo pone Tailscale (WireGuard) por debajo."
}

esperar_vivo() {
    local i ip
    ip="$(direccion_tailscale)"
    for ((i = 1; i <= 15; i++)); do
        if curl -s "http://$ip:$PUERTO/v2/" >/dev/null 2>&1; then
            info "responde HTTP 200 tras ${i}s"
            return 0
        fi
        sleep 1
    done
    error "el registry no respondió en 15s"
    docker logs "$CONTENEDOR" 2>&1 | tail -15 | sed 's/^/      /'
    return 1
}

estado() {
    local ip; ip="$(direccion_tailscale)"

    echo "contenedor:"
    docker ps --filter "name=$CONTENEDOR" --format '   {{.Names}}\t{{.Status}}\t{{.Ports}}' \
        || echo "   (no está corriendo)"

    echo
    echo "desde el tailnet ($ip:$PUERTO):"
    if curl -s "http://$ip:$PUERTO/v2/" >/dev/null 2>&1; then
        echo "   HTTP /v2/ -> OK (200)"
    else
        echo "   (no responde — revisar el -p y el firewall)"
        return 0
    fi

    echo
    echo "catálogo de imágenes:"
    local cat_json repos
    cat_json="$(curl -s "http://$ip:$PUERTO/v2/_catalog" || echo '{}')"
    repos="$(echo "$cat_json" | grep -o '"repositories":\[[^]]*\]' || true)"

    if [[ -n "$repos" ]]; then
        info "$cat_json"
        echo
        echo "tags por repositorio:"
        local lista_repos
        lista_repos="$(echo "$cat_json" | sed -n 's/.*"repositories":\[\([^]]*\)\].*/\1/p' | tr -d '"' | tr ',' ' ')"
        for r in $lista_repos; do
            [[ -z "$r" ]] && continue
            local tags_json
            tags_json="$(curl -s "http://$ip:$PUERTO/v2/$r/tags/list" || echo '{}')"
            info "  - $r: $tags_json"
        done
    else
        info "  (catálogo vacío o formato no parseable)"
    fi
}

bajar() {
    paso "BAJAR — el contenedor, no los datos"
    docker rm -f "$CONTENEDOR" >/dev/null 2>&1 || true
    info "$DIR_REGISTRY queda: las capas guardadas siguen en disco"
    info "para borrarlas de verdad: rm -rf $DIR_REGISTRY"
}

limpiar() {
    paso "LIMPIAR — garbage collect de capas huérfanas en el registry"
    if docker ps --filter "name=$CONTENEDOR" --format '{{.Names}}' | grep -q "$CONTENEDOR"; then
        docker exec "$CONTENEDOR" registry garbage-collect /etc/docker/registry/config.yml
        info "Garbage collection completado."
    else
        error "el contenedor $CONTENEDOR no está corriendo."
        exit 1
    fi
}

case "${1:-levantar}" in
    levantar) levantar ;;
    estado)   estado ;;
    bajar)    bajar ;;
    limpiar)  limpiar ;;
    *)
        awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "${BASH_SOURCE[0]}"
        exit 2
        ;;
esac
