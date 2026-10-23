# 0003 — VPS Hetzner para ingesta y procesamiento, R2 como almacenamiento definitivo

- Fecha: 2026-10-21
- Estado: aceptada (2026-10-23; descarga inicial: solo el grupo nacional, ver ADR 0002)

## Contexto

Las sesiones de desarrollo corren en contenedores efímeros: se borran por inactividad y cortan
cualquier proceso a las 2 h. La descarga inicial, el crawl de metadatos y la actualización
semanal necesitan una máquina persistente, y los datos tienen que sobrevivir a esa máquina.

## Decisión

- **Servidor:** Hetzner Cloud CPX con un volumen de 80–100 GB en la misma ubicación.
  - **Tamaño:** se recomienda **CPX32** (4 vCPU, 8 GB). El embudo carga en memoria el catálogo
    completo (853.316 series) y DuckDB reescribe buckets de Parquet; con 4 GB (CPX22) queda
    poco margen.
  - **Precios:** Hetzner los subió dos veces en 2026; hay que confirmarlos en hetzner.com al
    crear el servidor.
- **Datos:**
  - En el volumen (`/mnt/data`), no en el disco del servidor: el servidor se puede recrear sin
    perderlos.
  - **R2** es la copia definitiva (ADR 0002) y se sincroniza al cerrar cada tramo.
- **Usuarios y acceso:**
  - `ops`: administrador con sudo y acceso solo por llave SSH. root no puede entrar por SSH.
  - `trend`: sin root, sin contraseña y sin SSH; es dueño del código, los datos y los servicios.
  - Firewall `ufw`: solo SSH entrante.
  - `fail2ban` y actualizaciones de seguridad automáticas.
  - `.env` con dueño `trend` y modo `600`.
- **Procesos (systemd):**
  - `trend-initial-download.service` (`infra/run_tramos.sh`): catálogo → metadatos → embudo
    (una vez) → valores vigentes vía v2, 10 releases por tramo → vintages de la muestra, 200
    series por tramo → exportes. Sincroniza a R2 tras cada tramo.
    - Es reanudable: el progreso vive en `data/processed/ingest_state/` (estado por serie y
      cursor `v2_releases_done`).
    - Con `Restart=on-failure` reintenta tras cortes.
    - Al terminar deja `initial_download.done` y no vuelve a correr.
    - Solo baja los grupos de `download.groups`.
  - `trend-weekly-update.timer` → `trend-weekly-update.service` (`infra/weekly_update.sh`),
    domingo 06:13 UTC con `Persistent=true`: `update` → `crawl-meta --if-stale` → embudo →
    `observations` (series nuevas en el universo) → `vintages` → exportes → sync.
    - No corre hasta que termine la descarga inicial.
  - Ambos scripts toman un `flock` en el directorio de estado, así que nunca corren a la vez.
- **Aprovisionamiento:** `infra/provision.sh` es idempotente y verifica antes de cambiar.
  - **Volumen:** solo formatea si no tiene sistema de archivos (`blkid`) y lo monta por
    `/dev/disk/by-id` con `nofail`.
  - **sshd:** valida con `sshd -t` antes de recargar.
  - **Código:** clona o actualiza con `--ff-only`, sin pisar cambios locales.
  - **`.env`:** no lo sobrescribe si ya existe.

## Alternativas consideradas

- **GitHub Actions para todo:** cada job dura como máximo 6 h y no tiene disco persistente;
  sirve para la corrida semanal (Fase 7) pero no para la descarga inicial ni para trabajar
  sobre el Parquet completo.
- **Solo R2, sin volumen:** cada `upsert` reescribe buckets enteros, y hacerlo contra almacenamiento
  remoto multiplica la transferencia. El volumen local con sync por tramo es más simple y
  barato.

## Consecuencias

- Hay costo mensual fijo (servidor + volumen + R2) y una superficie a mantener (parches, llaves).
- La guía `docs/infra/vps.md` describe los pasos manuales: crear el servidor, el volumen, el
  bucket y las llaves. Todo lo demás lo hace `provision.sh`.
