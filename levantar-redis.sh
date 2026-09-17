#!/usr/bin/env bash
#
# Levanta la base compartida del servicio en esta casa.
#
#   ./levantar.sh            # levantar (o volver a levantar) la base
#   ./levantar.sh estado     # ¿está viva? ¿cuántas personas tiene?
#   ./levantar.sh bajar      # bajarla sin borrar los datos
#
# La base corre en UNA casa y la consultan las réplicas de todas las demás, así
# que tiene que escuchar en el tailnet y no en localhost. Publica el puerto sólo
# en la IP de Tailscale: desde fuera del tailnet no existe.

set -euo pipefail

RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONTENEDOR="${CONTENEDOR:-sdypp-redis}"
IMAGEN="${IMAGEN:-redis:8-alpine}"
PUERTO="${PUERTO:-6379}"

# Los datos van a un directorio del host, no a un volumen de Docker.
#
# Con un volumen hay que pelearse con los uid: la imagen trae /data de su propio
# usuario (999) y Docker se lo vuelve a aplicar cada vez que se monta mientras el
# volumen está vacío, así que un chown previo no sobrevive. Con un directorio del
# host el dueño es el que le pongamos, y de paso se puede mirar el AOF sin sudo,
# que es lo que hace verificable "la base persiste" en la demo.
#
# Fuera del repositorio, al lado del .env de las réplicas: datos de producción no
# viven en un árbol de git.
DIR_DATOS="${DIR_DATOS:-$HOME/sdypp/redis-datos}"

# La red de Docker donde ya corren las réplicas de desarrollo de esta misma
# máquina. Si no existe, la base igual queda accesible por la IP de Tailscale.
RED_LOCAL="${RED_LOCAL:-sdypp}"

ARCHIVO_ENV="${ARCHIVO_ENV:-$RAIZ/.env}"

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
    # `tailscale ip -4` puede devolver más de una línea si hay varias cuentas.
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

# --- La contraseña ---------------------------------------------------------

cargar_clave() {
    # El .env no se versiona (está en el .gitignore). Si no existe, se genera una
    # clave y se avisa: es mejor que una elegida a mano en el apuro de la demo,
    # que después termina siendo la misma de todo lo demás.
    if [[ ! -f "$ARCHIVO_ENV" ]]; then
        paso "No hay .env — generando una contraseña nueva"
        local generada
        generada="$(openssl rand -base64 24 2>/dev/null | tr -d '/+=' | head -c 32)"
        if [[ -z "$generada" ]]; then
            error "no pude generar la contraseña (falta openssl). Copiá .env.example a mano."
            exit 1
        fi
        printf 'REDIS_PASS=%s\n' "$generada" > "$ARCHIVO_ENV"
        chmod 600 "$ARCHIVO_ENV"
        info "escrita en $ARCHIVO_ENV (permisos 600, fuera del repo)"
    fi

    # shellcheck disable=SC1090
    source "$ARCHIVO_ENV"
    if [[ -z "${REDIS_PASS:-}" ]]; then
        error "$ARCHIVO_ENV no define REDIS_PASS"
        exit 1
    fi
}

# La contraseña no puede ir en redis.conf, que sí se versiona, ni en la línea del
# `docker run`, donde queda visible para cualquiera que haga `docker inspect` o
# mire la lista de procesos del host. Se arma un archivo derivado, con permisos
# 600 y fuera del repo, que se monta de sólo lectura.
armar_configuracion() {
    local destino="$RAIZ/redis.local.conf"
    {
        cat "$RAIZ/redis.conf"
        echo
        echo "# --- Agregado por levantar.sh desde el .env. NO SE VERSIONA. ---"
        echo "requirepass $REDIS_PASS"
    } > "$destino"
    chmod 600 "$destino"
    echo "$destino"
}

# --- Comandos --------------------------------------------------------------

levantar() {
    local ip configuracion
    ip="$(direccion_tailscale)"
    cargar_clave
    configuracion="$(armar_configuracion)"

    paso "LEVANTAR — la base en $ip:$PUERTO"

    # El directorio sobrevive al contenedor: `docker rm` no borra las personas,
    # que es lo que permite bajar y volver a levantar la base durante la demo.
    mkdir -p "$DIR_DATOS"
    docker rm -f "$CONTENEDOR" >/dev/null 2>&1 || true

    # Redis corre con TU uid, no con el que trae la imagen (999). Hace falta
    # porque el archivo de configuración lleva la contraseña y está en 600: si el
    # proceso de adentro fuera otro usuario, no podría leerlo, y la única salida
    # sería abrirle los permisos a todo el mundo.
    local duenio; duenio="$(id -u):$(id -g)"

    local -a red=()
    if docker network inspect "$RED_LOCAL" >/dev/null 2>&1; then
        red=(--network "$RED_LOCAL")
        info "también en la red Docker '$RED_LOCAL', para las réplicas de esta máquina"
    fi

    docker run -d \
        --name "$CONTENEDOR" \
        --restart unless-stopped \
        --user "$duenio" \
        "${red[@]}" \
        -p "$ip:$PUERTO:6379" \
        -v "$configuracion:/etc/redis/redis.conf:ro" \
        -v "$DIR_DATOS:/data" \
        --health-cmd "redis-cli -a \"\$REDISCLI_AUTH\" ping | grep -q PONG" \
        --health-interval 10s --health-timeout 3s --health-retries 3 \
        -e "REDISCLI_AUTH=$REDIS_PASS" \
        "$IMAGEN" redis-server /etc/redis/redis.conf >/dev/null

    esperar_viva
    paso "LISTA"
    info "Publicada SÓLO en $ip:$PUERTO — desde fuera del tailnet no responde."
    echo
    info "Lo que va en el ~/sdypp/.env de cada casa con una réplica:"
    printf '\n    TP_REDIS_URL=redis://:%s@%s:%s/0\n\n' "$REDIS_PASS" "$ip" "$PUERTO"
    aviso "esa línea lleva la contraseña: va por Discord, nunca al repositorio."
}

esperar_viva() {
    local i
    for ((i = 1; i <= 20; i++)); do
        if docker exec "$CONTENEDOR" redis-cli -a "$REDIS_PASS" --no-auth-warning ping 2>/dev/null | grep -q PONG; then
            info "responde PONG tras ${i}s"
            return 0
        fi
        sleep 1
    done
    error "la base no respondió en 20s"
    docker logs "$CONTENEDOR" 2>&1 | tail -15 | sed 's/^/      /'
    return 1
}

estado() {
    cargar_clave
    local ip; ip="$(direccion_tailscale)"

    echo "contenedor:"
    docker ps --filter "name=$CONTENEDOR" --format '   {{.Names}}\t{{.Status}}\t{{.Ports}}' \
        || echo "   (no está corriendo)"

    echo
    echo "desde adentro:"
    docker exec "$CONTENEDOR" redis-cli -a "$REDIS_PASS" --no-auth-warning ping 2>/dev/null \
        | sed 's/^/   ping -> /' || echo "   (no responde)"

    # Lo que de verdad importa: que responda por la IP del tailnet, que es por
    # donde la van a consultar las otras casas. Un PONG desde adentro del
    # contenedor no prueba nada sobre eso.
    echo
    echo "desde el tailnet ($ip):"
    docker run --rm "$IMAGEN" \
        redis-cli -h "$ip" -p "$PUERTO" -a "$REDIS_PASS" --no-auth-warning ping 2>/dev/null \
        | sed 's/^/   ping -> /' || echo "   (no responde — revisar el -p y el firewall)"

    echo
    echo "personas guardadas:"
    docker exec "$CONTENEDOR" redis-cli -a "$REDIS_PASS" --no-auth-warning \
        zcard personas:index 2>/dev/null | sed 's/^/   /' || echo "   (no se pudo consultar)"
}

bajar() {
    paso "BAJAR — el contenedor, no los datos"
    docker rm -f "$CONTENEDOR" >/dev/null 2>&1 || true
    info "$DIR_DATOS queda: al volver a levantar, las personas siguen ahí"
    info "para borrarlas de verdad: rm -rf $DIR_DATOS"
}

case "${1:-levantar}" in
    levantar) levantar ;;
    estado)   estado ;;
    bajar)    bajar ;;
    *)
        awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "${BASH_SOURCE[0]}"
        exit 2
        ;;
esac
