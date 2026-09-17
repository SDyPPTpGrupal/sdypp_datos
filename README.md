# La base compartida — Redis

El estado del servicio. Las réplicas de Python son *stateless*: las personas no
viven en la memoria de ninguna, viven acá. Eso es lo que hace que un alta la
pueda atender una casa y la lectura siguiente otra, y que matar una réplica no
pierda nada.

```
réplica Python (casa-salvador) ─┐
réplica Python (casa-meizers) ──┼──► sdypp-redis @ casa-tomas:6379
réplica Java   (…)          ────┘         (por Tailscale)
```

**Dónde corre y quién la opera** — el enunciado pide negociarlo y contarlo:
corre en **la casa de Tomás**, en un contenedor, y la opera Tomás. Se decidió así
porque la casa de Mateo Nomico, que la iba a hostear, estuvo caída; y porque es
la casa que ya tiene el balanceador y el CI/CD arriba todo el día, así que es la
que más disponibilidad real ofrece. **La contracara está abajo, en el SPOF.**

## Comandos

```bash
./levantar.sh            # levantar (o volver a levantar) la base
./levantar.sh estado     # ¿viva? ¿responde por el tailnet? ¿cuántas personas hay?
./levantar.sh bajar      # bajar el contenedor sin borrar los datos
```

La primera vez genera un `.env` con una contraseña aleatoria y te la muestra una
vez, ya armada como `TP_REDIS_URL`. **Esa línea va por Discord a cada casa que
tenga una réplica, y a ningún archivo del repositorio.**

## Comprobarla

```bash
./levantar.sh estado
```

Los tres chequeos que importan, y por qué el tercero no es redundante:

| | |
| :--- | :--- |
| `ping` desde adentro | el proceso arrancó |
| `ping` desde el tailnet | **es el que vale**: es por donde la consultan las otras casas. Un PONG desde adentro del contenedor no dice nada sobre si el puerto está publicado donde tiene que estar |
| `zcard personas:index` | cuántas personas hay guardadas |

Que de verdad no esté abierta al mundo:

```bash
ss -tln | grep 6379     # tiene que decir 100.101.15.93:6379, NUNCA 0.0.0.0:6379
```

## Decisiones

**Escucha sólo en la IP de Tailscale.** El `-p 100.101.15.93:6379:6379` publica el
puerto en esa interfaz y en ninguna otra: desde la LAN de casa o desde internet la
base no existe, ni siquiera para el handshake. Es más fuerte que confiar en la
contraseña, porque no le da a un atacante ni la oportunidad de intentar. Se
comprueba, no se afirma:

```bash
ss -tln | grep 6379                    # 100.101.15.93:6379, nada más
bash -c '</dev/tcp/192.168.100.15/6379'  # desde la LAN: conexión rehusada
```

El atajo de poner `-p 6379:6379` publica en **todas** las interfaces y hay que
evitarlo.

**Un puerto publicado por Docker pasa por `FORWARD`, no por `INPUT`.** Es lo que
nos costó la tarde del 07/09 y merece quedar escrito, porque el síntoma no apunta
al firewall. Con `-p`, el paquete no termina en un proceso del host: se le hace
DNAT hacia la IP del contenedor, así que lo evalúa la cadena `FORWARD`. Con el
`DEFAULT_FORWARD_POLICY="DROP"` que trae `ufw`, la base quedaba inalcanzable desde
las otras casas aunque `ss` mostrara el puerto escuchando y `tailscale ping`
respondiera —el ping lo contesta `tailscaled` en espacio de usuario, sin pasar por
netfilter—. Las reglas que hacen falta son de `route`, que es como `ufw` llama a
`FORWARD`:

```bash
sudo ufw route allow in  on tailscale0   # que entren a los contenedores
sudo ufw route allow out on tailscale0   # que los contenedores salgan al tailnet
sudo ufw allow in on tailscale0          # lo que sí es del host: sshd, --network host
```

Y el diagnóstico que ahorra el rodeo: si `CLIENT LIST` de Redis muestra sólo
clientes locales y `rejected_connections` está en 0, **el paquete no llegó nunca**.
No es la contraseña —una contraseña mal puesta se ve como una conexión aceptada y
un `NOAUTH`— ni es Redis: es la red.

**Contraseña obligatoria, y fuera del repositorio.** `requirepass` no está en
`redis.conf` —que sí se versiona y se puede publicar entero— sino en un `.env`
con permisos 600. `levantar.sh` arma al vuelo un `redis.local.conf` que es la
concatenación de los dos, y lo monta de sólo lectura. Tampoco va en la línea del
`docker run`, donde quedaría a la vista de cualquiera que haga `docker inspect` o
mire la lista de procesos del host.

**Redis corre con el uid del usuario, no con el 999 de la imagen.** Es la
consecuencia de lo anterior: si la configuración está en 600 y adentro corre otro
usuario, no la puede leer, y la salida fácil sería aflojarle los permisos al
archivo que tiene la contraseña

**Los datos van a `~/sdypp/redis-datos`, un directorio del host, no a un volumen
de Docker.** Con un volumen hay que pelearse con los uid: la imagen trae `/data`
de su propio usuario y **Docker le vuelve a aplicar ese dueño cada vez que se
monta mientras el volumen está vacío**, así que un `chown` previo no sobrevive al
primer arranque. Con un directorio del host el dueño es el que le pongamos. De
yapa se puede mirar el AOF sin `sudo`, que es lo que hace *verificable* la
persistencia en vez de sólo afirmarla

```bash
ls -la ~/sdypp/redis-datos/appendonlydir/
```

**Persistencia AOF + snapshots.** El enunciado pide que el estado sobreviva al
reparto entre réplicas; que sobreviva a un reinicio de la base es la otra mitad.
Sin `appendonly yes`, un `docker restart` borra todas las personas y la demo de
estado compartido se cae sin que nadie toque una réplica. `appendfsync everysec`
en vez de `always`: `always` hace un `fsync` por escritura y ahí el alta de una
persona pasa a costar lo que tarde el disco; `everysec` arriesga como mucho un
segundo de escrituras contra un corte de luz. Es un intercambio, no un descuido.

**`maxmemory-policy noeviction`.** La decisión más importante del `redis.conf`.
Con cualquier política de expiración, Redis al llenarse **borra personas viejas
para hacer lugar, sin avisar**: el servicio seguiría contestando 200 mientras
pierde datos. Con `noeviction` la escritura falla, la app devuelve `UNAVAILABLE`
y el problema se ve. Una base de registros no es un caché.

**El log sale por `docker logs`.** A diferencia de las réplicas y del balanceador,
la base no lleva bitácora propia con el formato del contrato: no atiende
operaciones del servicio, atiende comandos. Lo que hay que poder cruzar es
*quién* dio de alta a una persona, y eso está en las otras dos bitácoras.

## El SPOF cambió de lugar (picante 4)

Replicamos las apps y —en la Etapa 3— el balanceador. **La base quedó una sola.**
Si se cae esta casa, las réplicas siguen vivas y sanas, el balanceador sigue
repartiendo, y `/personas` devuelve `503` en todas: el servicio queda de pie pero
inútil. Se puede ver en vivo con `./levantar.sh bajar`.

No se replica igual de fácil que una app stateless, y por eso no lo hicimos:

- Una réplica de app se clona porque no tiene nada que sincronizar. Dos Redis
  tienen que ponerse de acuerdo sobre **qué escritura pasó antes**, y ahí aparece
  todo lo que la materia ve después (consenso, quórum, particiones).
- `redis-server --replicaof` da una réplica de **lectura**, que no es lo que hace
  falta: el problema es que las escrituras van a un solo lugar. Para failover de
  escritura hace falta Redis Sentinel (elección de líder) o Cluster (sharding).
- Y ahí aparece el CAP de Brewer, que el TP 3 pide citar: ante una partición,
  Sentinel elige **consistencia** y deja de aceptar escrituras. El servicio se
  frena antes que aceptar dos altas con el mismo `id`. Que es, exactamente, el
  problema que hoy nos está resolviendo gratis el hecho de que haya una sola.

Lo honesto para la demo es **mostrarlo, no taparlo**: la base es hoy el punto
único de falla del sistema, y sabemos por qué no lo arreglamos todavía.

## Y una que no es picante pero muerde

La base guarda las personas de **las dos apps**, Java y Python, bajo el mismo
esquema de claves (`persona:<id>`, `personas:index`, `personas:seq`,
`legajo:<legajo>`). Ese esquema es tan contrato como el `.proto`: si una de las
dos implementaciones guardara la misma persona bajo otra clave, las dos
escribirían en la misma base sin encontrar nunca lo del otro. Está en
`CONTRATO.md §6` del repo de la app.
