#!/usr/bin/env python3
"""
Administración Unificada de la Máquina de Datos (Redis + Docker Registry)
========================================================================
Herramienta multiplataforma (Windows / Linux / macOS) en Python puro.

USO DESDE LA CONSOLA:
    python datos.py                     # Levanta Redis y Registry a la vez
    python datos.py estado              # Diagnóstico y contenido de ambos servicios
    python datos.py bajar               # Apaga ambos contenedores (datos en disco persisten)
    python datos.py limpiar             # Ejecuta garbage-collect en el Registry

Subcomandos específicos:
    python datos.py redis [levantar|estado|bajar]
    python datos.py registry [levantar|estado|bajar|limpiar]
"""

import argparse
import json
import os
import platform
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# --- Formato de Consola (Colores ANSI) ---------------------------------------

USE_COLOR = sys.stdout.isatty() or os.name == "nt"


def paso(msg: str):
    if USE_COLOR:
        print(f"\n\033[1;34m========================================================\033[0m")
        print(f"\033[1;34m==> {msg}\033[0m")
        print(f"\033[1;34m========================================================\033[0m")
    else:
        print(f"\n==> {msg}")


def info(msg: str):
    print(f"    {msg}")


def aviso(msg: str):
    if USE_COLOR:
        print(f"\033[33m    ! {msg}\033[0m")
    else:
        print(f"    ! {msg}")


def error(msg: str):
    if USE_COLOR:
        print(f"\033[31m!!! {msg}\033[0m", file=sys.stderr)
    else:
        print(f"!!! {msg}", file=sys.stderr)


# --- Configuración y Directorios ---------------------------------------------

if getattr(sys, "frozen", False):
    RAIZ = Path(sys.executable).resolve().parent
    # Si dist/datos.exe está dentro de dist/, pero el repo está un nivel arriba:
    if (RAIZ.parent / "redis.conf").exists():
        RAIZ = RAIZ.parent
else:
    RAIZ = Path(__file__).resolve().parent
HOME = Path.home()

CONTENEDOR_REDIS = os.getenv("CONTENEDOR_REDIS", "sdypp-redis")
IMAGEN_REDIS = os.getenv("IMAGEN_REDIS", "redis:8-alpine")
PUERTO_REDIS = os.getenv("PUERTO_REDIS", "6379")
DIR_DATOS_REDIS = Path(os.getenv("DIR_DATOS_REDIS", HOME / "sdypp" / "redis-datos"))
RED_LOCAL = os.getenv("RED_LOCAL", "sdypp")
ARCHIVO_ENV = Path(os.getenv("ARCHIVO_ENV", RAIZ / ".env"))

CONTENEDOR_REGISTRY = os.getenv("CONTENEDOR_REGISTRY", "sdypp-registry")
IMAGEN_REGISTRY = os.getenv("IMAGEN_REGISTRY", "registry:2")
PUERTO_REGISTRY = os.getenv("PUERTO_REGISTRY", "5000")
DIR_REGISTRY = Path(os.getenv("DIR_REGISTRY", HOME / "sdypp" / "registry"))


# --- Auxiliares de Red y Sistema --------------------------------------------

def obtener_ip_tailscale() -> str:
    """Averigua la IP IPv4 de Tailscale en Linux/Windows/macOS."""
    ip_env = os.getenv("IP_TAILSCALE")
    if ip_env:
        return ip_env.strip()

    comandos_tailscale = [
        ["tailscale", "ip", "-4"],
        ["tailscale.exe", "ip", "-4"],
        [r"C:\Program Files\Tailscale\tailscale.exe", "ip", "-4"],
    ]

    for cmd in comandos_tailscale:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if res.returncode == 0 and res.stdout.strip():
                ip = res.stdout.strip().splitlines()[0].strip()
                if ip:
                    return ip
        except FileNotFoundError:
            continue

    error("No pude averiguar la IP de Tailscale de esta máquina.")
    info("¿Está corriendo? Probá 'tailscale status'.")
    info("Si sabés cuál es, pasala como variable: IP_TAILSCALE=100.x.y.z python datos.py")
    sys.exit(1)


def obtener_args_duenio() -> list:
    """Devuelve la opción --user adecuada según el sistema operativo."""
    if platform.system() == "Windows":
        # En Windows, Docker Desktop con WSL2 gestiona permisos de volumen automáticamente.
        return []
    else:
        try:
            uid = os.getuid()
            gid = os.getgid()
            return ["--user", f"{uid}:{gid}"]
        except AttributeError:
            return []


def cargar_o_generar_clave_redis() -> str:
    """Carga o genera REDIS_PASS en .env."""
    if not ARCHIVO_ENV.exists():
        paso("No hay .env — generando una contraseña nueva")
        # Genera clave de 32 caracteres alfanuméricos seguros
        raw_token = secrets.token_urlsafe(32)
        clave = re.sub(r"[^a-zA-Z0-9]", "", raw_token)[:32]
        with open(ARCHIVO_ENV, "w", encoding="utf-8") as f:
            f.write(f"REDIS_PASS={clave}\n")
        if platform.system() != "Windows":
            try:
                os.chmod(ARCHIVO_ENV, 0o600)
            except OSError:
                pass
        info(f"Escrita en {ARCHIVO_ENV} (fuera del repo)")

    redis_pass = None
    with open(ARCHIVO_ENV, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("REDIS_PASS="):
                redis_pass = line.split("=", 1)[1].strip()
                break

    if not redis_pass:
        error(f"{ARCHIVO_ENV} no define REDIS_PASS")
        sys.exit(1)

    return redis_pass


def armar_configuracion_redis(redis_pass: str) -> Path:
    """Genera redis.local.conf con requirepass sin versionarlo."""
    base_conf = RAIZ / "redis.conf"
    destino = RAIZ / "redis.local.conf"

    contenido_base = ""
    if base_conf.exists():
        contenido_base = base_conf.read_text(encoding="utf-8")

    nuevo_contenido = (
        f"{contenido_base}\n\n"
        f"# --- Agregado por datos.py desde el .env. NO SE VERSIONA. ---\n"
        f"requirepass {redis_pass}\n"
    )

    destino.write_text(nuevo_contenido, encoding="utf-8")
    if platform.system() != "Windows":
        try:
            os.chmod(destino, 0o600)
        except OSError:
            pass

    return destino


# --- Módulo REDIS -----------------------------------------------------------

class ModuloRedis:
    @staticmethod
    def levantar():
        ip = obtener_ip_tailscale()
        redis_pass = cargar_o_generar_clave_redis()
        configuracion = armar_configuracion_redis(redis_pass)

        paso(f"LEVANTAR REDIS — la base en {ip}:{PUERTO_REDIS}")

        DIR_DATOS_REDIS.mkdir(parents=True, exist_ok=True)
        subprocess.run(["docker", "rm", "-f", CONTENEDOR_REDIS], capture_output=True)

        duenio_args = obtener_args_duenio()

        # Verificar red docker local
        red_args = []
        inspect = subprocess.run(["docker", "network", "inspect", RED_LOCAL], capture_output=True)
        if inspect.returncode == 0:
            red_args = ["--network", RED_LOCAL]
            info(f"También en la red Docker '{RED_LOCAL}', para las réplicas de esta máquina")

        cmd_run = [
            "docker", "run", "-d",
            "--name", CONTENEDOR_REDIS,
            "--restart", "unless-stopped",
            *duenio_args,
            *red_args,
            "-p", f"{ip}:{PUERTO_REDIS}:6379",
            "-v", f"{configuracion}:/etc/redis/redis.conf:ro",
            "-v", f"{DIR_DATOS_REDIS}:/data",
            "--health-cmd", 'redis-cli -a "$REDISCLI_AUTH" ping | grep -q PONG',
            "--health-interval", "10s",
            "--health-timeout", "3s",
            "--health-retries", "3",
            "-e", f"REDISCLI_AUTH={redis_pass}",
            IMAGEN_REDIS,
            "redis-server", "/etc/redis/redis.conf"
        ]

        res = subprocess.run(cmd_run, capture_output=True, text=True)
        if res.returncode != 0:
            error(f"Error al ejecutar docker run: {res.stderr}")
            sys.exit(1)

        ModuloRedis.esperar_viva(redis_pass)
        paso("REDIS LISTO")
        info(f"Publicada SÓLO en {ip}:{PUERTO_REDIS} — desde fuera del tailnet no responde.")
        print()
        info("Lo que va en el ~/sdypp/.env de cada casa con una réplica:")
        print(f"\n    TP_REDIS_URL=redis://:{redis_pass}@{ip}:{PUERTO_REDIS}/0\n")
        aviso("Esa línea lleva la contraseña: va por Discord, nunca al repositorio.")

    @staticmethod
    def esperar_viva(redis_pass: str):
        for i in range(1, 21):
            cmd = ["docker", "exec", CONTENEDOR_REDIS, "redis-cli", "-a", redis_pass, "--no-auth-warning", "ping"]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if "PONG" in res.stdout:
                info(f"Responde PONG tras {i}s")
                return True
            time.sleep(1)

        error("La base Redis no respondió en 20s")
        logs = subprocess.run(["docker", "logs", CONTENEDOR_REDIS], capture_output=True, text=True)
        lines = logs.stdout.splitlines()[-15:]
        for line in lines:
            print(f"      {line}")
        return False

    @staticmethod
    def estado():
        redis_pass = cargar_o_generar_clave_redis()
        ip = obtener_ip_tailscale()

        print("contenedor:")
        res = subprocess.run(
            ["docker", "ps", "--filter", f"name={CONTENEDOR_REDIS}", "--format", "   {{.Names}}\t{{.Status}}\t{{.Ports}}"],
            capture_output=True, text=True
        )
        if res.stdout.strip():
            print(res.stdout.strip())
        else:
            print("   (no está corriendo)")

        print("\ndesde adentro:")
        cmd_in = ["docker", "exec", CONTENEDOR_REDIS, "redis-cli", "-a", redis_pass, "--no-auth-warning", "ping"]
        res_in = subprocess.run(cmd_in, capture_output=True, text=True)
        if "PONG" in res_in.stdout:
            print("   ping -> PONG")
        else:
            print("   (no responde)")

        print(f"\ndesde el tailnet ({ip}):")
        cmd_tail = [
            "docker", "run", "--rm", IMAGEN_REDIS,
            "redis-cli", "-h", ip, "-p", PUERTO_REDIS, "-a", redis_pass, "--no-auth-warning", "ping"
        ]
        res_tail = subprocess.run(cmd_tail, capture_output=True, text=True)
        if "PONG" in res_tail.stdout:
            print("   ping -> PONG")
        else:
            print("   (no responde — revisar el -p y el firewall)")

        print("\npersonas guardadas:")
        cmd_zcard = ["docker", "exec", CONTENEDOR_REDIS, "redis-cli", "-a", redis_pass, "--no-auth-warning", "zcard", "personas:index"]
        res_zcard = subprocess.run(cmd_zcard, capture_output=True, text=True)
        if res_zcard.returncode == 0 and res_zcard.stdout.strip():
            print(f"   {res_zcard.stdout.strip()}")
        else:
            print("   (no se pudo consultar)")

    @staticmethod
    def bajar():
        paso("BAJAR REDIS — el contenedor, no los datos")
        subprocess.run(["docker", "rm", "-f", CONTENEDOR_REDIS], capture_output=True)
        info(f"{DIR_DATOS_REDIS} queda: al volver a levantar, las personas siguen ahí")
        info(f"para borrarlas de verdad: rm -rf {DIR_DATOS_REDIS}")


# --- Módulo DOCKER REGISTRY --------------------------------------------------

class ModuloRegistry:
    @staticmethod
    def levantar():
        ip = obtener_ip_tailscale()
        paso(f"LEVANTAR REGISTRY — el almacén en {ip}:{PUERTO_REGISTRY}")

        DIR_REGISTRY.mkdir(parents=True, exist_ok=True)
        subprocess.run(["docker", "rm", "-f", CONTENEDOR_REGISTRY], capture_output=True)

        duenio_args = obtener_args_duenio()

        cmd_run = [
            "docker", "run", "-d",
            "--name", CONTENEDOR_REGISTRY,
            "--restart", "unless-stopped",
            *duenio_args,
            "-p", f"{ip}:{PUERTO_REGISTRY}:5000",
            "-v", f"{DIR_REGISTRY}:/var/lib/registry",
            "-e", "REGISTRY_STORAGE_DELETE_ENABLED=true",
            "--health-cmd", "wget -qO- http://127.0.0.1:5000/v2/ >/dev/null || exit 1",
            "--health-interval", "10s",
            "--health-timeout", "3s",
            "--health-retries", "3",
            IMAGEN_REGISTRY
        ]

        res = subprocess.run(cmd_run, capture_output=True, text=True)
        if res.returncode != 0:
            error(f"Error al ejecutar docker run: {res.stderr}")
            sys.exit(1)

        ModuloRegistry.esperar_vivo(ip)
        paso("REGISTRY LISTO")
        info(f"Publicado SÓLO en {ip}:{PUERTO_REGISTRY} — desde fuera del tailnet no responde.")
        print()
        info("Configuración requerida en /etc/docker/daemon.json de las casas y devs:")
        print(f'\n    {{"insecure-registries": ["{ip}:{PUERTO_REGISTRY}"]}}\n')
        aviso("Sin TLS/Auth porque el cifrado lo pone Tailscale (WireGuard) por debajo.")

    @staticmethod
    def esperar_vivo(ip: str):
        url = f"http://{ip}:{PUERTO_REGISTRY}/v2/"
        for i in range(1, 16):
            try:
                req = urllib.request.Request(url)
                with urllib.request.urlopen(req, timeout=2) as resp:
                    if resp.status == 200:
                        info(f"Responde HTTP 200 tras {i}s")
                        return True
            except Exception:
                pass
            time.sleep(1)

        error("El registry no respondió en 15s")
        logs = subprocess.run(["docker", "logs", CONTENEDOR_REGISTRY], capture_output=True, text=True)
        lines = logs.stdout.splitlines()[-15:]
        for line in lines:
            print(f"      {line}")
        return False

    @staticmethod
    def estado():
        ip = obtener_ip_tailscale()

        print("contenedor:")
        res = subprocess.run(
            ["docker", "ps", "--filter", f"name={CONTENEDOR_REGISTRY}", "--format", "   {{.Names}}\t{{.Status}}\t{{.Ports}}"],
            capture_output=True, text=True
        )
        if res.stdout.strip():
            print(res.stdout.strip())
        else:
            print("   (no está corriendo)")

        print(f"\ndesde el tailnet ({ip}:{PUERTO_REGISTRY}):")
        url_catalog = f"http://{ip}:{PUERTO_REGISTRY}/v2/_catalog"
        try:
            req = urllib.request.Request(f"http://{ip}:{PUERTO_REGISTRY}/v2/")
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    print("   HTTP /v2/ -> OK (200)")
        except Exception:
            print("   (no responde — revisar el -p y el firewall)")
            return

        print("\ncatálogo de imágenes:")
        try:
            with urllib.request.urlopen(url_catalog, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                repos = data.get("repositories", [])
                if repos:
                    info(json.dumps(data))
                    print("\ntags por repositorio:")
                    for repo in repos:
                        url_tags = f"http://{ip}:{PUERTO_REGISTRY}/v2/{repo}/tags/list"
                        try:
                            with urllib.request.urlopen(url_tags, timeout=3) as t_resp:
                                t_data = json.loads(t_resp.read().decode("utf-8"))
                                info(f"  - {repo}: {json.dumps(t_data)}")
                        except Exception as e:
                            info(f"  - {repo}: (error obteniendo tags: {e})")
                else:
                    info("  (catálogo vacío)")
        except Exception as e:
            info(f"  (no se pudo obtener el catálogo: {e})")

    @staticmethod
    def bajar():
        paso("BAJAR REGISTRY — el contenedor, no los datos")
        subprocess.run(["docker", "rm", "-f", CONTENEDOR_REGISTRY], capture_output=True)
        info(f"{DIR_REGISTRY} queda: las capas guardadas siguen en disco")
        info(f"para borrarlas de verdad: rm -rf {DIR_REGISTRY}")

    @staticmethod
    def limpiar():
        paso("LIMPIAR REGISTRY — garbage collect de capas huérfanas")
        res = subprocess.run(
            ["docker", "ps", "--filter", f"name={CONTENEDOR_REGISTRY}", "--format", "{{.Names}}"],
            capture_output=True, text=True
        )
        if CONTENEDOR_REGISTRY in res.stdout:
            gc_res = subprocess.run(
                ["docker", "exec", CONTENEDOR_REGISTRY, "registry", "garbage-collect", "/etc/docker/registry/config.yml"],
                capture_output=True, text=True
            )
            print(gc_res.stdout)
            info("Garbage collection completado.")
        else:
            error(f"El contenedor {CONTENEDOR_REGISTRY} no está corriendo.")
            sys.exit(1)


# --- ORQUESTADOR UNIFICADO ---------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Administración de la Máquina de Datos (Redis + Registry)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python datos.py                 Levanta Redis y Registry simultáneamente
  python datos.py estado          Diagnóstico completo de Redis y Registry
  python datos.py bajar           Detiene ambos contenedores
  python datos.py limpiar         Ejecuta garbage-collect en el Registry
  python datos.py redis estado    Diagnóstico exclusivo de Redis
  python datos.py registry bajar  Detiene únicamente el Registry
        """
    )

    parser.add_argument(
        "target_or_action",
        nargs="?",
        default="all",
        help="Componente ('all', 'redis', 'registry') o Acción ('levantar', 'estado', 'bajar', 'limpiar')"
    )

    parser.add_argument(
        "action_opt",
        nargs="?",
        default=None,
        help="Acción ('levantar', 'estado', 'bajar', 'limpiar') cuando se especifica componente"
    )

    args = parser.parse_args()

    posibles_acciones = {"levantar", "estado", "bajar", "limpiar"}

    if args.target_or_action in posibles_acciones:
        componente = "all"
        accion = args.target_or_action
    else:
        componente = args.target_or_action.lower()
        accion = (args.action_opt or "levantar").lower()

    if componente not in {"all", "redis", "registry"}:
        error(f"Componente no válido: '{componente}'. Opciones: all, redis, registry")
        sys.exit(1)

    if accion not in posibles_acciones:
        error(f"Acción no válida: '{accion}'. Opciones: levantar, estado, bajar, limpiar")
        sys.exit(1)

    # --- Ejecución ---

    if componente == "all":
        if accion == "levantar":
            paso("INICIANDO SERVICIOS DE DATOS (REDIS + REGISTRY)")
            ModuloRedis.levantar()
            print()
            ModuloRegistry.levantar()
            paso("TODOS LOS SERVICIOS DE DATOS ESTÁN EN MARCHA")
        elif accion == "estado":
            paso("ESTADO DE REDIS (BD DE ESTADO)")
            ModuloRedis.estado()
            print()
            paso("ESTADO DEL REGISTRY (ALMACÉN DE IMÁGENES)")
            ModuloRegistry.estado()
        elif accion == "bajar":
            paso("BAJANDO SERVICIOS DE DATOS (REDIS + REGISTRY)")
            ModuloRedis.bajar()
            print()
            ModuloRegistry.bajar()
            paso("AMBOS SERVICIOS FUERON DETENIDOS (LOS DATOS PERMANECEN EN DISCO)")
        elif accion == "limpiar":
            paso("LIMPIEZA DE CAPAS HUÉRFANAS EN REGISTRY")
            ModuloRegistry.limpiar()

    elif componente == "redis":
        if accion == "levantar":
            ModuloRedis.levantar()
        elif accion == "estado":
            ModuloRedis.estado()
        elif accion == "bajar":
            ModuloRedis.bajar()
        elif accion == "limpiar":
            aviso("Redis no requiere garbage collection. Operación reservada para Registry.")

    elif componente == "registry":
        if accion == "levantar":
            ModuloRegistry.levantar()
        elif accion == "estado":
            ModuloRegistry.estado()
        elif accion == "bajar":
            ModuloRegistry.bajar()
        elif accion == "limpiar":
            ModuloRegistry.limpiar()


if __name__ == "__main__":
    main()
