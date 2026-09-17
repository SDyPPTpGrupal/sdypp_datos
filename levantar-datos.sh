#!/usr/bin/env bash

#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#
# SCRIPT DE ADMINISTRACIÓN UNIFICADO: MÁQUINA DE DATOS (REDIS + DOCKER REGISTRY)
#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#

# ¿QUÉ HACE ESTE ARCHIVO?
# Este script permite administrar los DOS servicios de la Máquina de Datos al mismo tiempo:
#   1. Base de datos Redis (sdypp-redis) en puerto 6379.
#   2. Almacén de imágenes privadas (sdypp-registry) en puerto 5000.
#
# USO DESDE LA CONSOLA:
#   ./levantar-datos.sh            # Levanta o reinicia Redis y Registry a la vez.
#   ./levantar-datos.sh estado     # Muestra el diagnóstico y contenido de ambos servicios.
#   ./levantar-datos.sh bajar      # Apaga ambos contenedores (los datos en disco persisten).
#   ./levantar-datos.sh limpiar    # Ejecuta el recolector de basura del Registry.
#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#

set -euo pipefail

# Obtiene la ruta de la carpeta donde se encuentra este script.
RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

paso() { printf '\n\033[1;34m========================================================\n==> %s\n========================================================\033[0m\n' "$*"; }

case "${1:-levantar}" in
    levantar)
        paso "INICIANDO SERVICIOS DE DATOS (REDIS + REGISTRY)"
        "$RAIZ/levantar-redis.sh" levantar
        echo
        "$RAIZ/levantar-registry.sh" levantar
        paso "TODOS LOS SERVICIOS DE DATOS ESTÁN EN MARCHA"
        ;;
    estado)
        paso "ESTADO DE REDIS (BD DE ESTADO)"
        "$RAIZ/levantar-redis.sh" estado
        echo
        paso "ESTADO DEL REGISTRY (ALMACÉN DE IMÁGENES)"
        "$RAIZ/levantar-registry.sh" estado
        ;;
    bajar)
        paso "BAJANDO SERVICIOS DE DATOS (REDIS + REGISTRY)"
        "$RAIZ/levantar-redis.sh" bajar
        echo
        "$RAIZ/levantar-registry.sh" bajar
        paso "AMBOS SERVICIOS FUERON DETENIDOS (LOS DATOS PERMANECEN EN DISCO)"
        ;;
    limpiar)
        paso "LIMPIEZA DE CAPAS HUÉRFANAS EN REGISTRY"
        "$RAIZ/levantar-registry.sh" limpiar
        ;;
    *)
        awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' "${BASH_SOURCE[0]}"
        exit 2
        ;;
esac
