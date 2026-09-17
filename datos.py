#!/usr/bin/env python3
"""
#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#
# SCRIPT DE ADMINISTRACIÓN UNIFICADO: MÁQUINA DE DATOS (REDIS + DOCKER REGISTRY)
#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#

Infraestructura de almacenamiento y distribución del servicio (Máquina de Datos sdypp_datos).
Esta máquina aloja dos servicios centrales:

1. Redis (`sdypp-redis` @ 6379):
   - El estado del servicio. Las réplicas de Python y Java son stateless; la información
     de las personas no vive en la memoria de ninguna réplica, vive acá.

2. Registry (`sdypp-registry` @ 5000):
   - El registro de imágenes Docker privado (registry:2) donde 'publicar.sh' sube (docker push)
     las nuevas imágenes y desde donde las casas de las réplicas ejecutan docker pull
     (ordenadas por el CD vía SSH) para descargar sólo la capa que cambió.

---------------------------------------------------------------------------------------------------
FLUJO DEL SISTEMA SEGÚN DISEÑO DE ARQUITECTURA (Excalidraw):
---------------------------------------------------------------------------------------------------
 1. PUBLICACIÓN: El desarrollador desde su máquina corre 'publicar.sh':
    - Sube la imagen armada con 'docker push 100.78.246.64:5000/sdypp-app-python:vX'
      directamente a este Registry. Sólo viajan las capas nuevas.
    - Envía el 'manifiesto.json' al CD (puerto 2222 en Plataforma).

 2. DESPLIEGUE: El CD recibe el manifiesto y se conecta por SSH a cada casa (sin usar scp)
    para ordenarles las 3 siguientes cosas:
    1- Ejecutar 'docker pull 100.78.246.64:5000/...'
    2- Descargar sólo la capa que cambió desde este Registry.

SEGURIDAD Y RED:
 - No usa TLS ni autenticación previa en el Registry porque la seguridad de extremo a extremo
   la provee Tailscale (WireGuard subyacente). Es el mismo principio por el cual gRPC no usa TLS
   dentro del tailnet.
 - Tanto Redis como el Registry publican sus puertos ÚNICAMENTE en la IP de Tailscale (100.x.y.z).
   Desde la red LAN local o internet, los puertos no existen ni responden al handshake TCP.

USO DESDE LA CONSOLA (Windows / Linux / macOS):
    python datos.py                     # Levanta Redis y Registry en un solo paso.
    python datos.py estado              # Estado detallado de ambos servicios y contenido guardado.
    python datos.py bajar               # Apaga ambos contenedores sin borrar datos del disco.
    python datos.py limpiar             # Ejecuta el garbage-collect de capas huérfanas en el Registry.

SUBCOMANDOS ESPECÍFICOS:
    python datos.py redis [levantar|estado|bajar]
    python datos.py registry [levantar|estado|bajar|limpiar]
#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#=#
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

# --- FORMATO DE CONSOLA Y SALIDA DE TEXTO (COLORES ANSI) --------------------

# Detecta si la terminal soporta salida con colores (TTY o consola de Windows)
USE_COLOR = sys.stdout.isatty() or os.name == "nt"


def paso(msg: str):
    """Imprime un título principal destacado con separadores visuales."""
    if USE_COLOR:
        print(f"\n\033[1;34m========================================================\033[0m")
        print(f"\033[1;34m==> {msg}\033[0m")
        print(f"\033[1;34m========================================================\033[0m")
    else:
        print(f"\n========================================================\n==> {msg}\n========================================================")


def info(msg: str):
    """Imprime información secundaria con sangría."""
    print(f"    {msg}")


def aviso(msg: str):
    """Imprime un aviso de advertencia destacado en color amarillo (!)."""
    if USE_COLOR:
        print(f"\033[33m    ! {msg}\033[0m")
    else:
        print(f"    ! {msg}")


def error(msg: str):
    """Imprime un error grave en color rojo (!!!) hacia la salida de errores estándar."""
    if USE_COLOR:
        print(f"\033[31m!!! {msg}\033[0m", file=sys.stderr)
    else:
        print(f"!!! {msg}", file=sys.stderr)


# --- CONFIGURACIÓN Y VARIABLES GENERALES -------------------------------------

# Obtiene la ruta física del proyecto (manejando tanto ejecución como script .py o ejecutable compilado .exe)
if getattr(sys, "frozen", False):
    RAIZ = Path(sys.executable).resolve().parent
    if (RAIZ.parent / "redis.conf").exists():
        RAIZ = RAIZ.parent
else:
    RAIZ = Path(__file__).resolve().parent

HOME = Path.home()

# --- Configuración de Redis ---
# Nombre del contenedor en Docker (por defecto 'sdypp-redis')
CONTENEDOR_REDIS = os.getenv("CONTENEDOR_REDIS", "sdypp-redis")
# Imagen oficial de Redis Hub
IMAGEN_REDIS = os.getenv("IMAGEN_REDIS", "redis:8-alpine")
# Puerto en el que escuchará Redis (por defecto 6379)
PUERTO_REDIS = os.getenv("PUERTO_REDIS", "6379")

# Los datos van a un directorio del host, no a un volumen de Docker.
# Con un volumen hay que pelearse con los uid: la imagen trae /data de su propio
# usuario (999) y Docker se lo vuelve a aplicar cada vez que se monta mientras el
# volumen está vacío, así que un chown previo no sobrevive. Con un directorio del
# host el dueño es el que le pongamos, y de paso se puede mirar el AOF sin sudo,
# que es lo que hace verificable "la base persiste" en la demo.
# Fuera del repositorio, al lado del .env de las réplicas: datos de producción no viven en un árbol de git.
DIR_DATOS_REDIS = Path(os.getenv("DIR_DATOS_REDIS", HOME / "sdypp" / "redis-datos"))

# La red de Docker donde ya corren las réplicas de desarrollo de esta misma máquina
RED_LOCAL = os.getenv("RED_LOCAL", "sdypp")
ARCHIVO_ENV = Path(os.getenv("ARCHIVO_ENV", RAIZ / ".env"))

# --- Configuración del Registry ---
# Nombre del contenedor en Docker (por defecto 'sdypp-registry')
CONTENEDOR_REGISTRY = os.getenv("CONTENEDOR_REGISTRY", "sdypp-registry")
# Imagen oficial de Docker Hub para crear la bodega (Registry versión 2)
IMAGEN_REGISTRY = os.getenv("IMAGEN_REGISTRY", "registry:2")
# Puerto de red en el que escuchará el almacén (por defecto el puerto 5000)
PUERTO_REGISTRY = os.getenv("PUERTO_REGISTRY", "5000")

# Carpeta en el disco rígido de la máquina de Datos donde se guardan las capas de imágenes.
# Tal como indica el Excalidraw: capas en ~/sdypp/registry
DIR_REGISTRY = Path(os.getenv("DIR_REGISTRY", HOME / "sdypp" / "registry"))


# --- FUNCIONES AUXILIARES DE RED Y SISTEMA OPERATIVO ------------------------

def obtener_ip_tailscale() -> str:
    """
    Averigua la dirección IP única (v4) de esta computadora dentro de la red privada Tailscale.
    Busca automáticamente en Linux, macOS y Windows (incluyendo la ruta estándar C:\\Program Files\\Tailscale\\tailscale.exe).
    """
    # 1. Si el usuario ya pasó manualmente una IP (ej. IP_TAILSCALE=100.x.y.z), usamos esa.
    ip_env = os.getenv("IP_TAILSCALE")
    if ip_env:
        return ip_env.strip()

    # 2. Le preguntamos al comando 'tailscale' cuál es nuestra IP de versión 4.
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

    # 3. Si no encuentra ninguna IP (porque Tailscale está apagado), detiene la ejecución con error.
    error("No pude averiguar la IP de Tailscale de esta máquina.")
    info("¿Está corriendo? Probá 'tailscale status'.")
    info("Si sabés cuál es, pasala como variable: IP_TAILSCALE=100.x.y.z python datos.py")
    sys.exit(1)


def obtener_args_duenio() -> list:
    """
    Determina los permisos de usuario (--user) según el sistema operativo:
    - En Linux/macOS: Retorna [--user, "UID:GID"] para que los archivos del host no queden bloqueados como root.
    - En Windows: Retorna [] (sin --user) porque Docker Desktop gestiona los permisos automáticamente vía WSL2 host.
    """
    if platform.system() == "Windows":
        return []
    else:
        try:
            uid = os.getuid()
            gid = os.getgid()
            return ["--user", f"{uid}:{gid}"]
        except AttributeError:
            return []


def cargar_o_generar_clave_redis() -> str:
    """
    Carga la contraseña de Redis desde el archivo .env.
    Si no existe el .env, genera una clave aleatoria segura de 32 caracteres y la escribe con permisos 600.
    """
    # El .env no se versiona (está en el .gitignore). Si no existe, se genera una
    # clave y se avisa: es mejor que una elegida a mano en el apuro de la demo,
    # que después termina siendo la misma de todo lo demás.
    if not ARCHIVO_ENV.exists():
        paso("No hay .env — generando una contraseña nueva")
        raw_token = secrets.token_urlsafe(32)
        clave = re.sub(r"[^a-zA-Z0-9]", "", raw_token)[:32]
        with open(ARCHIVO_ENV, "w", encoding="utf-8") as f:
            f.write(f"REDIS_PASS={clave}\n")
        if platform.system() != "Windows":
            try:
                os.chmod(ARCHIVO_ENV, 0o600)
            except OSError:
                pass
        info(f"Escrita en {ARCHIVO_ENV} (permisos 600, fuera del repo)")

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
    """
    La contraseña no puede ir en redis.conf (que sí se versiona), ni en la línea del
    `docker run` (donde queda visible para cualquiera que haga `docker inspect` o mire
    la lista de procesos del host). Se arma un archivo derivado (redis.local.conf), con
    permisos 600 y fuera del repo, que se monta de sólo lectura.
    """
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


# =============================================================================
# MÓDULO REDIS: ADMINISTRACIÓN DE LA BASE DE DATOS DEL ESTADO (`sdypp-redis`)
# =============================================================================

class ModuloRedis:
    @staticmethod
    def levantar():
        """Crea y pone en marcha el contenedor de la base de datos Redis."""
        ip = obtener_ip_tailscale()
        redis_pass = cargar_o_generar_clave_redis()
        configuracion = armar_configuracion_redis(redis_pass)

        paso(f"LEVANTAR REDIS — la base en {ip}:{PUERTO_REDIS}")

        # El directorio sobrevive al contenedor: `docker rm` no borra las personas,
        # que es lo que permite bajar y volver a levantar la base durante la demo.
        DIR_DATOS_REDIS.mkdir(parents=True, exist_ok=True)
        subprocess.run(["docker", "rm", "-f", CONTENEDOR_REDIS], capture_output=True)

        duenio_args = obtener_args_duenio()

        # Verificar si la red local 'sdypp' existe en Docker para unir el contenedor
        red_args = []
        inspect = subprocess.run(["docker", "network", "inspect", RED_LOCAL], capture_output=True)
        if inspect.returncode == 0:
            red_args = ["--network", RED_LOCAL]
            info(f"También en la red Docker '{RED_LOCAL}', para las réplicas de esta máquina")

        # Explicación de los parámetros de `docker run`:
        #   -d : Ejecuta el contenedor en segundo plano (detached).
        #   --name : Asigna el nombre 'sdypp-redis'.
        #   --restart unless-stopped : Se reinicia automáticamente si se reinicia la máquina.
        #   -p ip:6379:6379 : REGLA DE SEGURIDAD: Publica el puerto 6379 ÚNICAMENTE en la IP de Tailscale.
        #   -v configuracion:... : Monta redis.local.conf de sólo lectura (:ro).
        #   -v DIR_DATOS:... : Monta la carpeta del host en /data (persistencia).
        #   --health-cmd : Comprueba cada 10s que Redis responda PONG a la autenticación.
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
    def esperar_viva(redis_pass: str) -> bool:
        """Espera hasta 20 segundos comprobando si Redis responde PONG."""
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
        """Muestra el diagnóstico completo de Redis: estado del contenedor, ping interno/tailnet y cantidad de personas."""
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

        # Lo que de verdad importa: que responda por la IP del tailnet, que es por
        # donde la van a consultar las otras casas. Un PONG desde adentro del
        # contenedor no prueba nada sobre eso.
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
        """Apaga y borra el contenedor Docker, manteniendo intactos los datos en disco (DIR_DATOS_REDIS)."""
        paso("BAJAR REDIS — el contenedor, no los datos")
        subprocess.run(["docker", "rm", "-f", CONTENEDOR_REDIS], capture_output=True)
        info(f"{DIR_DATOS_REDIS} queda: al volver a levantar, las personas siguen ahí")
        info(f"para borrarlas de verdad: rm -rf {DIR_DATOS_REDIS}")


# =============================================================================
# MÓDULO REGISTRY: REGISTRO PRIVADO DE IMÁGENES DOCKER (`sdypp-registry`)
# =============================================================================

class ModuloRegistry:
    @staticmethod
    def levantar():
        """Crea y pone en marcha el contenedor del registro privado de imágenes."""
        ip = obtener_ip_tailscale()
        paso(f"LEVANTAR REGISTRY — el almacén en {ip}:{PUERTO_REGISTRY}")

        # Crea la carpeta física ~/sdypp/registry en el disco duro si todavía no existe.
        DIR_REGISTRY.mkdir(parents=True, exist_ok=True)

        # Si ya existía un contenedor viejo con el mismo nombre, lo elimina para arrancar de cero.
        subprocess.run(["docker", "rm", "-f", CONTENEDOR_REGISTRY], capture_output=True)

        duenio_args = obtener_args_duenio()

        # Explicación de los parámetros de `docker run` para el Registry:
        #   -d : Ejecuta el contenedor en segundo plano (en silencio).
        #   --name : Asigna el nombre 'sdypp-registry'.
        #   --restart unless-stopped : Si la computadora se reinicia, Docker vuelve a levantarlo.
        #   -p ip:5000:5000 : REGLA DE SEGURIDAD CLAVE: Publica el puerto 5000 ÚNICAMENTE en la IP de Tailscale.
        #   -v DIR_REGISTRY:/var/lib/registry : Guarda las capas de imágenes en ~/sdypp/registry.
        #   -e REGISTRY_STORAGE_DELETE_ENABLED=true : Permite garbage collection de capas huérfanas.
        #   --health-cmd : Comprueba cada 10s que la API /v2/ del registry esté sana.
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
    def esperar_vivo(ip: str) -> bool:
        """Comprueba repetidamente durante 15 segundos si la API HTTP /v2/ del Registry responde."""
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
        """Muestra en consola el estado del contenedor, respuesta HTTP y el catálogo con sus tags/imágenes guardadas."""
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

        # Consulta la lista de imágenes guardadas en el almacén (Catálogo)
        print("\ncatálogo de imágenes:")
        try:
            with urllib.request.urlopen(url_catalog, timeout=3) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                repos = data.get("repositories", [])
                if repos:
                    info(json.dumps(data))
                    print("\ntags por repositorio:")
                    # Extrae los nombres de cada aplicación guardada y consulta sus versiones (tags).
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
        """Detiene y borra el contenedor Docker, manteniendo a salvo las capas en ~/sdypp/registry."""
        paso("BAJAR REGISTRY — el contenedor, no los datos")
        subprocess.run(["docker", "rm", "-f", CONTENEDOR_REGISTRY], capture_output=True)
        info(f"{DIR_REGISTRY} queda: las capas guardadas siguen en disco")
        info(f"para borrarlas de verdad: rm -rf {DIR_REGISTRY}")

    @staticmethod
    def limpiar():
        """Ejecuta el recolector de basura (garbage collection) para borrar capas huérfanas en el disco."""
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


# =============================================================================
# ORQUESTADOR PRINCIPAL DE LÍNEA DE COMANDOS (CLI)
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Administración de la Máquina de Datos (Redis + Registry)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos de uso:
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

    # Evalúa si la primera opción es una acción o un componente objetivo
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

    # --- Ejecución de Comandos ---

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
