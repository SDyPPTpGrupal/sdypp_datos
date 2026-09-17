#!/usr/bin/env bash

#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#
# SCRIPT DE ADMINISTRACIÓN: REGISTRO PRIVADO DE IMÁGENES DOCKER (DOCKER REGISTRY)
#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#

# El flujo del sistema:

# 1. PUBLICACIÓN: El desarrollador desde su máquina corre 'publicar.sh':
#    - Sube la imagen armada con 'docker push 100.78.246.64:5000/sdypp-app-python:vX'
#      directamente a este Registry y Sólo viajan las capas nuevas.
#    - Envía el 'manifiesto.json' al CD (puerto 2222 en Plataforma).

# 2. DESPLIEGUE: El CD recibe el manifiesto y se conecta por SSH a cada casa (sin usar scp)
#    para ordenarles las 3 siguientes cosas:
#    1- Ejecutar 'docker pull 100.78.246.64:5000/...'
#    2- Descargar sólo la capa que cambió desde este Registry.

# USO DEL SCRIPT DESDE LA CONSOLA:
#   ./levantar-registry.sh            # Enciende o reinicia el almacén de imágenes.
#   ./levantar-registry.sh estado     # Verifica si responde y muestra las imágenes y tags guardados.
#   ./levantar-registry.sh bajar      # Apaga el contenedor sin borrar las imágenes del disco (en ~/sdypp/registry).
#   ./levantar-registry.sh limpiar    # Ejecuta garbage-collect de capas huérfanas para liberar disco.

# SEGURIDAD Y RED:
# No usa TLS ni autenticación directa porque la seguridad la provee Tailscale.

#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#

# 'set -euo pipefail' es un modo de máxima seguridad en Bash:
#   -e : Si algún comando falla, el script se detiene inmediatamente (no sigue a ciegas).
#   -u : Si intentamos usar una variable no definida, detiene la ejecución.
#   -o pipefail : Si falla un comando dentro del pipe, falla todo el pipe.
set -euo pipefail

# Obtiene la ruta de la carpeta donde está guardado este archivo.
RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- CONFIGURACIÓN Y VARIABLES GENERALES -------------------------------------

# Nombre del contenedor en Docker (por defecto 'sdypp-registry').
CONTENEDOR="${CONTENEDOR:-sdypp-registry}"

# Imagen oficial de Docker Hub para crear la bodega (Registry versión 2).
IMAGEN="${IMAGEN:-registry:2}"

# Puerto de red en el que escuchará el almacén (por defecto el puerto 5000).
PUERTO="${PUERTO:-5000}"

# Carpeta en el disco rígido de la máquina de Datos (Nomico) donde se guardan las capas de imágenes.
# Tal como indica el Excalidraw: capas en ~/sdypp/registry
DIR_REGISTRY="${DIR_REGISTRY:-$HOME/sdypp/registry}"

# --- FUNCIONES AUXILIARES PARA FORMATO DE TEXTO -------------------------------

# Imprime un título principal en formato destacado (negrita).
paso()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# Imprime información secundaria con sangría.
info()  { printf '    %s\n' "$*"; }

# Imprime un aviso de advertencia en color amarillo (!).
aviso() { printf '\033[33m    ! %s\033[0m\n' "$*"; }

# Imprime un error grave en color rojo (!!!) hacia la salida de errores.
error() { printf '\033[31m!!! %s\033[0m\n' "$*" >&2; }

# --- OBTENCIÓN DE LA DIRECCIÓN IP DE RED TAILSCALE ----------------------------

# Esta función averigua la IP única de la computadora dentro de la red Tailscale.
direccion_tailscale() {
    # 1. Si el usuario ya pasó manualmente una IP (ej. IP_TAILSCALE=100.x.y.z), usamos esa.
    if [[ -n "${IP_TAILSCALE:-}" ]]; then
        echo "$IP_TAILSCALE"
        return
    fi

    # 2. Le preguntamos al comando 'tailscale' cuál es nuestra IP de versión 4 (IPv4).
    local ip
    ip="$(tailscale ip -4 2>/dev/null | head -1 || true)"

    # 3. Si no encuentra ninguna IP (porque Tailscale está apagado), detiene el script con error.
    if [[ -z "$ip" ]]; then
        error "no pude averiguar la IP de Tailscale de esta máquina."
        info  "¿Está corriendo? Probá 'tailscale status'."
        info  "Si sabés cuál es, pasala a mano: IP_TAILSCALE=100.x.y.z $0"
        exit 1
    fi

    # 4. Devolvemos la IP encontrada.
    echo "$ip"
}

# --- COMANDO PRINCIPAL: LEVANTAR ----------------------------------------------

# Esta función crea y pone en marcha el contenedor del almacén de imágenes.
levantar() {
    local ip
    # Averiguamos la IP de la red privada donde debe escuchar (100.x.y.z).
    ip="$(direccion_tailscale)"

    paso "LEVANTAR — el registry en $ip:$PUERTO"

    # Crea la carpeta física ~/sdypp/registry en el disco duro si todavía no existe.
    mkdir -p "$DIR_REGISTRY"

    # Si ya existía un contenedor viejo con el mismo nombre, lo elimina para arrancar de cero.
    docker rm -f "$CONTENEDOR" >/dev/null 2>&1 || true

    # Obtiene identificador de usuario de Linux para que los archivos
    # guardados no queden bloqueados como propiedad de root.
    local duenio; duenio="$(id -u):$(id -g)"

    # Orden para crear y ejecutar el contenedor Docker:
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
        # Explicación de los parámetros del comando docker run arriba según Excalidraw:
        #   -d : Ejecuta el contenedor en segundo plano (en silencio).
        #   --name : Le asigna el nombre 'sdypp-registry'.
        #   --restart unless-stopped : Si la computadora se reinicia, Docker vuelve a levantarlo.
        #   --user "$duenio" : Corre con tu mismo usuario para no causar problemas de permisos.
        #   -p "$ip:$PUERTO:5000" : REGLA DE SEGURIDAD CLAVE: Publica el puerto 5000 ÚNICAMENTE
        #                           en la IP de Tailscale (100.78.246.64). Desde fuera no existe.
        #   -v "$DIR_REGISTRY:/var/lib/registry" : Guarda las capas de imágenes en ~/sdypp/registry.
        #   -e REGISTRY_STORAGE_DELETE_ENABLED=true : Permite garbage collection de capas huérfanas.
        #   --health-cmd : Comprueba cada 10s que la API /v2/ del registry esté sana.

    # Llama a la función que espera hasta que el servicio esté respondiendo.
    esperar_vivo

    paso "LISTO"
    info "Publicado SÓLO en $ip:$PUERTO — desde fuera del tailnet no responde."
    echo
    info "Configuración requerida en /etc/docker/daemon.json de las casas y devs:"
    printf '\n    {"insecure-registries": ["%s:%s"]}\n\n' "$ip" "$PUERTO"
    aviso "Sin TLS/Auth porque el cifrado lo pone Tailscale (WireGuard) por debajo."
}

# --- FUNCIÓN DE ESPERA Y VERIFICACIÓN -----------------------------------------

# Comprueba repetidamente durante 15 segundos si el almacén ya está listo.
esperar_vivo() {
    local i ip
    ip="$(direccion_tailscale)"

    # Hace 15 intentos (uno por segundo).
    for ((i = 1; i <= 15; i++)); do
        # Intenta conectarse a la dirección HTTP del almacén.
        if curl -s "http://$ip:$PUERTO/v2/" >/dev/null 2>&1; then
            info "responde HTTP 200 tras ${i}s"
            return 0  # Si respondió bien, termina con éxito.
        fi
        sleep 1  # Espera 1 segundo antes de volver a probar.
    done

    # Si pasaron 15 segundos y no respondió, muestra un error y las últimas líneas del log.
    error "el registry no respondió en 15s"
    docker logs "$CONTENEDOR" 2>&1 | tail -15 | sed 's/^/      /'
    return 1
}

# --- COMANDO: ESTADO ----------------------------------------------------------

# Muestra en consola el estado del contenedor y qué imágenes hay guardadas.
estado() {
    local ip; ip="$(direccion_tailscale)"

    # 1. Comprueba si la caja Docker está corriendo.
    echo "contenedor:"
    docker ps --filter "name=$CONTENEDOR" --format '   {{.Names}}\t{{.Status}}\t{{.Ports}}' \
        || echo "   (no está corriendo)"

    echo
    # 2. Prueba conectarse a través de la red Tailscale.
    echo "desde el tailnet ($ip:$PUERTO):"
    if curl -s "http://$ip:$PUERTO/v2/" >/dev/null 2>&1; then
        echo "   HTTP /v2/ -> OK (200)"
    else
        echo "   (no responde — revisar el -p y el firewall)"
        return 0
    fi

    echo
    # 3. Consulta la lista de imágenes guardadas en el almacén (Catálogo).
    echo "catálogo de imágenes:"
    local cat_json repos
    cat_json="$(curl -s "http://$ip:$PUERTO/v2/_catalog" || echo '{}')"
    repos="$(echo "$cat_json" | grep -o '"repositories":\[[^]]*\]' || true)"

    if [[ -n "$repos" ]]; then
        info "$cat_json"
        echo
        echo "tags por repositorio:"
        # Extrae los nombres de cada aplicación guardada y consulta sus versiones (tags).
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

# --- COMANDO: BAJAR -----------------------------------------------------------

# Detiene y borra el contenedor Docker, manteniendo a salvo las capas en ~/sdypp/registry.
bajar() {
    paso "BAJAR — el contenedor, no los datos"
    docker rm -f "$CONTENEDOR" >/dev/null 2>&1 || true
    info "$DIR_REGISTRY queda: las capas guardadas siguen en disco"
    info "para borrarlas de verdad: rm -rf $DIR_REGISTRY"
}

# --- COMANDO: LIMPIAR ---------------------------------------------------------

# Ejecuta el recolector de basura (garbage collection) para borrar capas huérfanas en el disco.
limpiar() {
    paso "LIMPIAR — garbage collect de capas huérfanas en el registry"
    # Verifica que el contenedor esté prendido antes de ejecutar la limpieza interna.
    if docker ps --filter "name=$CONTENEDOR" --format '{{.Names}}' | grep -q "$CONTENEDOR"; then
        docker exec "$CONTENEDOR" registry garbage-collect /etc/docker/registry/config.yml
        info "Garbage collection completado."
    else
        error "el contenedor $CONTENEDOR no está corriendo."
        exit 1
    fi
}

# --- SELECTOR PRINCIPAL DE COMANDOS (CASE) -----------------------------------

# Evalúa la primera palabra ingresada al ejecutar el script (ej. ./levantar-registry.sh estado).
# Si no se pasa ninguna palabra, ejecuta por defecto el comando 'levantar'.
case "${1:-levantar}" in
    levantar) levantar ;;
    estado)   estado ;;
    bajar)    bajar ;;
    limpiar)  limpiar ;;
    *)
        # Si se ingresa una opción no válida, muestra los comentarios de ayuda del inicio.
        awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "${BASH_SOURCE[0]}"
        exit 2
        ;;
esac
