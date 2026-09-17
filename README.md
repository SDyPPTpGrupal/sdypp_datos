# La máquina de Datos — Redis + Registry (`sdypp_datos`)

Infraestructura de almacenamiento y distribución del servicio. Esta máquina aloja dos servicios centrales:

1. **Redis (`sdypp-redis`):** El estado del servicio. Las réplicas de Python y Java son *stateless*; la información de las personas no vive en la memoria de ninguna réplica, vive acá.
2. **Registry (`sdypp-registry`):** El registro de imágenes Docker privado (`registry:2`) donde `publicar.sh` sube (`docker push`) las nuevas imágenes y desde donde las **casas de las réplicas** ejecutan `docker pull` (ordenadas por el CD vía SSH) para descargar sólo la capa que cambió.

```
 Dev (publicar.sh) ──► docker push ──┐
                                     ▼
 Casas (réplicas)  ──► docker pull ──► sdypp-registry @ 100.78.246.64:5000 (Imágenes de réplicas)
 Casas (réplicas)  ──► TCP 6379 ────► sdypp-redis    @ 100.78.246.64:6379 (Estado compartido)
```

**Dónde corre y quién la opera:**
Corre en la **máquina de Nomico (`100.78.246.64`)**, en contenedores Docker independientes. Ambos servicios se publican **únicamente en la IP de Tailscale**.

---

## Comandos

### Base de Datos (Redis)

```bash
./levantar.sh            # levantar (o volver a levantar) la base
./levantar.sh estado     # ¿viva? ¿responde por el tailnet? ¿cuántas personas hay?
./levantar.sh bajar      # bajar el contenedor sin borrar los datos
```

La primera vez genera un `.env` con una contraseña aleatoria y la muestra como `TP_REDIS_URL`. **Esa línea va por Discord a cada casa con réplicas, nunca al repositorio.**

### Registro de Imágenes (Docker Registry)

```bash
./levantar-registry.sh            # levantar (o volver a levantar) el registry
./levantar-registry.sh estado     # ¿está vivo? ¿qué imágenes y tags contiene?
./levantar-registry.sh bajar      # bajar el contenedor sin borrar las capas
./levantar-registry.sh limpiar    # garbage-collect de capas huérfanas
```

Configuración requerida en `/etc/docker/daemon.json` en las casas y entornos de desarrollo:
```json
{"insecure-registries": ["100.78.246.64:5000"]}
```

---

## Comprobar los Servicios

```bash
./levantar.sh estado
./levantar-registry.sh estado
```

Para verificar que **ningún** servicio esté expuesto públicamente a la LAN o internet:

```bash
ss -tln | grep 6379     # Debe mostrar 100.78.246.64:6379, NUNCA 0.0.0.0:6379
ss -tln | grep 5000     # Debe mostrar 100.78.246.64:5000, NUNCA 0.0.0.0:5000
```

---

## Decisiones de Diseño

### 1. ¿Por qué van juntos en la misma máquina?
Ambos componentes son servicios de **almacenamiento persistente** con patrones de acceso similares (escribe uno / leen muchos). Comparten los mismos requerimientos de administración: persistencia en disco del host, respaldo de datos y configuración de firewall de red.

### 2. Escucha exclusivamente en la IP de Tailscale
Tanto `-p 100.78.246.64:6379:6379` como `-p 100.78.246.64:5000:5000` publican los puertos **únicamente** en la interfaz de Tailscale. Desde la LAN local o internet, los puertos no existen ni responden al handshake TCP.

### 3. Registry sin TLS y sin Autenticación
- **Sin TLS:** El cifrado de extremo a extremo lo provee la capa de red subyacente de Tailscale (WireGuard). Es el mismo principio por el cual gRPC no usa TLS dentro del tailnet.
- **Sin Autenticación previa:** El registry es accesible solo para los miembros del tailnet. La seguridad del despliegue se garantiza porque el CD le ordena a las casas hacer `docker pull` de imágenes exclusivamente por **digest sha256** (`docker pull imagen@sha256:...`), lo que impide que un `push` malicioso altere lo que ya fue publicado y validado.

### 4. Persistencia en directorios del Host (No volúmenes Docker)
- Redis: `~/sdypp/redis-datos`
- Registry: `~/sdypp/registry`

Evita problemas de permisos de UID con imágenes Docker Alpine (`redis:8-alpine` y `registry:2`) y permite consultar/respaldar archivos (como la bitácora AOF de Redis o capas del registry) de forma directa.

### 5. Reglas de Firewall (`ufw route`)
Los puertos publicados por Docker atraviesan la cadena `FORWARD` de netfilter en lugar de `INPUT`. Por ello se requiere configurar explícitamente:

```bash
sudo ufw route allow in  on tailscale0   # Permitir tráfico entrante a contenedores
sudo ufw route allow out on tailscale0   # Permitir tráfico saliente de contenedores
sudo ufw allow in on tailscale0          # Tráfico directo al host (sshd)
```

---

## Punto Único de Falla (SPOF)

La máquina de **Datos (`100.78.246.64`) es un Punto Único de Falla (SPOF)** del sistema:

- Si la máquina cae, Redis no responde y las peticiones a la API devuelven `503`.
- Las réplicas existentes continuarán corriendo, pero no podrán realizar operaciones de lectura/escritura de personas.
- El CD no podrá descargar nuevas imágenes del registry para hacer despliegues o conmutaciones.

> **Nota para el informe:** Aunque en la Etapa 3 se duplica el balanceador para eliminar el SPOF en el plano de control/datos de entrada, la máquina de Datos permanece como componente centralizado. Para eliminar por completo este SPOF en fases futuras se requeriría replicación activa y consenso en Redis (p.ej. Redis Sentinel / Cluster) y alta disponibilidad en el registro de imágenes.
