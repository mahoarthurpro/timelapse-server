from flask import Flask, request, jsonify
import subprocess, os, requests, shutil, logging, sys

logging.basicConfig(stream=sys.stdout, level=logging.INFO, force=True)
logger = logging.getLogger(__name__)

app = Flask(__name__)

AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN", "")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "")
AIRTABLE_TABLE_NAME = os.environ.get("AIRTABLE_TABLE_NAME", "Timelapse")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "mon_secret_123")
STORAGE_DIR = "/tmp/timelapse_jobs"
os.makedirs(STORAGE_DIR, exist_ok=True)


def get_job_dir(record_id):
    d = os.path.join(STORAGE_DIR, record_id)
    os.makedirs(d, exist_ok=True)
    return d


def create_timelapse(job_dir, output_path, fps, width, height):
    # Récupérer les fichiers avec leur nom original pour trier par date+heure
    meta_files = []
    for f in os.listdir(job_dir):
        if f.startswith("frame_") and f.endswith(".jpg"):
            name_path = os.path.join(job_dir, f.replace(".jpg", ".name"))
            if os.path.exists(name_path):
                with open(name_path) as nf:
                    original_name = nf.read().strip()
                meta_files.append((original_name, f))
            else:
                meta_files.append((f, f))

    # Tri par nom original (contient date+heure ex: IMG_0211.JPG)
    meta_files.sort(key=lambda x: x[0])
    files = [x[1] for x in meta_files]

    logger.info(f"Assemblage {len(files)} images triees par date, {width}x{height}")

    if len(files) < 2:
        logger.error(f"Pas assez d images: {len(files)}")
        return False

    # Log de l'ordre pour verification
    for i, (orig, frame) in enumerate(meta_files[:5]):
        logger.info(f"  {i+1}. {orig} -> {frame}")
    if len(meta_files) > 5:
        logger.info(f"  ... ({len(meta_files)} images au total)")

    # Renuméroter séquentiellement pour FFmpeg
    logger.info("Renumerotation des fichiers...")
    new_files = []
    for i, fname in enumerate(files):
        old_path = os.path.join(job_dir, fname)
        new_name = f"seq_{i:05d}.jpg"
        new_path = os.path.join(job_dir, new_name)
        os.rename(old_path, new_path)
        new_files.append(new_name)
    files = new_files
    logger.info(f"Renumerotation terminee: {len(files)} fichiers")

    # FPS sécurisé
    if fps <= 0:
        fps = 5
    logger.info(f"FPS utilise: {fps}, duree par frame: {1.0/fps}")

    # Créer le fichier concat
    concat_file = os.path.join(job_dir, f"concat_{width}.txt")
    with open(concat_file, "w") as f:
        for fname in files:
            fpath = os.path.join(job_dir, fname)
            f.write(f"file '{fpath}'\n")
            f.write(f"duration {1.0/fps}\n")
        # Répéter la dernière frame pour éviter bug FFmpeg
        f.write(f"file '{os.path.join(job_dir, files[-1])}'\n")

    # Log du concat
    logger.info("Contenu concat (3 premieres lignes):")
    with open(concat_file) as f:
        lines = f.readlines()
        for line in lines[:6]:
            logger.info(f"  {line.strip()}")

    vf = (
    f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
    f"setsar=1"
    )
    cmd = [
    "ffmpeg", "-y",
    "-threads", "1",
    "-f", "concat",
    "-safe", "0",
    "-i", concat_file,
    "-vf", vf,
    "-c:v", "libx264",
    "-crf", "18",
    "-preset", "fast",
    "-pix_fmt", "yuv420p",
    "-movflags", "+faststart",
    output_path
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.error(f"FFmpeg stderr complet:\n{result.stderr}")
        return False

    size = os.path.getsize(output_path)
    logger.info(f"Video creee: {size} bytes")
    return size > 1000


def upload_to_airtable(video_path, record_id, field_name):
    logger.info(f"Upload champ '{field_name}'...")
    try:
        with open(video_path, "rb") as f:
            r = requests.post(
                "https://tmpfiles.org/api/v1/upload",
                files={"file": f},
                timeout=120
            )
        if r.status_code != 200:
            logger.error(f"tmpfiles erreur: {r.text}")
            return False
        tmp_url = r.json()["data"]["url"].replace("tmpfiles.org/", "tmpfiles.org/dl/")
        logger.info(f"URL: {tmp_url}")

        url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}/{record_id}"
        headers = {
            "Authorization": f"Bearer {AIRTABLE_TOKEN}",
            "Content-Type": "application/json"
        }
        r2 = requests.patch(
            url,
            headers=headers,
            json={"fields": {field_name: [{"url": tmp_url}]}},
            timeout=30
        )
        if r2.status_code == 200:
            logger.info(f"Airtable OK!")
            return True
        logger.error(f"Airtable erreur: {r2.text}")
        return False
    except Exception as e:
        logger.error(f"Exception upload: {e}")
        return False


@app.route("/add-image", methods=["POST"])
def add_image():
    data = request.get_json()
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Non autorise"}), 401

    record_id = data.get("record_id", "")
    image_url = data.get("image_url", "")
    image_index = int(data.get("image_index", 1))
    total_images = int(data.get("total_images", 1))
    fps = int(data.get("fps", 5))
    if fps <= 0:
        fps = 5
    field_16_9 = data.get("airtable_field_16_9", "Video Youtube")
    field_9_16 = data.get("airtable_field_9_16", "Video Reels")
    file_name = data.get("file_name", f"frame_{image_index:05d}.jpg")

    logger.info(f"FPS utilisé: {fps}")
    logger.info(f"Image {image_index}/{total_images} | {file_name} | record={record_id}")
    job_dir = get_job_dir(record_id)

    # Télécharger la photo
    try:
        r = requests.get(image_url, timeout=30)
        r.raise_for_status()
        logger.info(f"Content-Type: {r.headers.get('Content-Type')}")
        logger.info(f"Status: {r.status_code}")
        logger.info(f"First bytes: {r.content[:20]}")
        logger.info(f"Taille image: {len(r.content)} octets")
        frame_path = os.path.join(job_dir, f"frame_{image_index:05d}.jpg")
        with open(frame_path, "wb") as f:
            f.write(r.content)

        # Sauvegarder le nom original pour trier par date+heure
        name_path = os.path.join(job_dir, f"frame_{image_index:05d}.name")
        with open(name_path, "w") as f:
            f.write(file_name)

        logger.info(f"Image {image_index} sauvegardee ({len(r.content)} bytes) -> tri: {file_name}")
    except Exception as e:
        logger.error(f"Erreur download: {e}")
        return jsonify({"error": str(e)}), 500

    # Dernière image -> créer le timelapse
    if image_index >= total_images:
        logger.info(f"Derniere image recue! Creation du timelapse...")
        results = {}

        out_169 = os.path.join(job_dir, "tl_169.mp4")
        out_916 = os.path.join(job_dir, "tl_916.mp4")

        if create_timelapse(job_dir, out_169, fps, 1920, 1080):
            upload_to_airtable(out_169, record_id, field_16_9)
            results["16_9"] = "ok"
        else:
            results["16_9"] = "erreur"

        if create_timelapse(job_dir, out_916, fps, 1080, 1920):
            upload_to_airtable(out_916, record_id, field_9_16)
            results["9_16"] = "ok"
        else:
            results["9_16"] = "erreur"

        shutil.rmtree(job_dir, ignore_errors=True)
        logger.info(f"Termine: {results}")
        return jsonify({"status": "done", "results": results}), 200

    return jsonify({"status": "saved", "progress": f"{image_index}/{total_images}"}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
