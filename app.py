from flask import Flask, request, jsonify
import subprocess
import os
import requests
import tempfile
import shutil
import logging
import sys
 
# Logging visible dans Railway Deploy Logs
logging.basicConfig(stream=sys.stdout, level=logging.INFO, force=True)
logger = logging.getLogger(__name__)
 
app = Flask(__name__)
 
AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN", "")
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "")
AIRTABLE_TABLE_NAME = os.environ.get("AIRTABLE_TABLE_NAME", "Timelapse")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "mon_secret_123")
 
 
def download_images(image_urls, folder):
    paths = []
    for i, url in enumerate(image_urls):
        filename = os.path.join(folder, f"frame_{i:05d}.jpg")
        try:
            logger.info(f"Téléchargement image {i+1}/{len(image_urls)}: {url[:80]}")
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            with open(filename, "wb") as f:
                f.write(response.content)
            size = os.path.getsize(filename)
            logger.info(f"✅ Image {i+1} téléchargée ({size} bytes)")
            paths.append(filename)
        except Exception as e:
            logger.error(f"❌ Erreur image {i+1}: {e}")
    return paths
 
 
def create_timelapse(input_folder, output_path, fps, width, height):
    # Lister les fichiers réellement téléchargés
    files = sorted([f for f in os.listdir(input_folder) if f.endswith('.jpg')])
    logger.info(f"Fichiers trouvés: {len(files)}")
 
    if len(files) == 0:
        logger.error("❌ Aucun fichier image trouvé!")
        return False
 
    # Créer un fichier concat pour FFmpeg (plus fiable que le pattern)
    concat_file = os.path.join(input_folder, "concat.txt")
    with open(concat_file, "w") as f:
        for fname in files:
            fpath = os.path.join(input_folder, fname)
            duration = 1.0 / fps
            f.write(f"file '{fpath}'\n")
            f.write(f"duration {duration}\n")
        # Répéter la dernière frame pour éviter le bug de fin
        if files:
            fpath = os.path.join(input_folder, files[-1])
            f.write(f"file '{fpath}'\n")
 
    vf_filter = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
    )
 
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_file,
        "-vf", vf_filter,
        "-c:v", "libx264",
        "-crf", "18",
        "-preset", "fast",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        output_path
    ]
 
    logger.info(f"Lancement FFmpeg pour {width}x{height}...")
    result = subprocess.run(cmd, capture_output=True, text=True)
 
    if result.returncode != 0:
        logger.error(f"❌ Erreur FFmpeg: {result.stderr[-500:]}")
        return False
 
    size = os.path.getsize(output_path)
    logger.info(f"✅ Vidéo créée: {output_path} ({size} bytes)")
    return size > 1000  # Vérifie que la vidéo n'est pas vide
 
 
def upload_to_airtable(video_path, record_id, field_name):
    logger.info(f"Upload vers Airtable champ '{field_name}'...")
    try:
        with open(video_path, "rb") as f:
            upload_response = requests.post(
                "https://tmpfiles.org/api/v1/upload",
                files={"file": f},
                timeout=60
            )
        logger.info(f"tmpfiles status: {upload_response.status_code}")
        if upload_response.status_code != 200:
            logger.error(f"❌ Upload tmpfiles échoué: {upload_response.text}")
            return False
 
        tmp_url = upload_response.json()["data"]["url"].replace(
            "tmpfiles.org/", "tmpfiles.org/dl/"
        )
        logger.info(f"URL temporaire: {tmp_url}")
 
        url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}/{record_id}"
        headers = {
            "Authorization": f"Bearer {AIRTABLE_TOKEN}",
            "Content-Type": "application/json"
        }
        payload = {"fields": {field_name: [{"url": tmp_url}]}}
        response = requests.patch(url, headers=headers, json=payload, timeout=30)
 
        if response.status_code == 200:
            logger.info(f"✅ Vidéo envoyée dans Airtable!")
            return True
        else:
            logger.error(f"❌ Erreur Airtable: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        logger.error(f"❌ Exception upload: {e}")
        return False
 
 
@app.route("/create-timelapse", methods=["POST"])
def create_timelapse_endpoint():
    data = request.get_json()
    logger.info(f"📨 Requête reçue")
 
    if data.get("secret") != WEBHOOK_SECRET:
        logger.error("❌ Secret invalide")
        return jsonify({"error": "Non autorisé"}), 401
 
    image_urls = data.get("images", [])
    record_id = data.get("record_id", "")
    fps = int(data.get("fps", 5))
    field_16_9 = data.get("airtable_field_16_9", "Video Youtube")
    field_9_16 = data.get("airtable_field_9_16", "Video Reels")
 
    logger.info(f"📸 {len(image_urls)} images, {fps} fps, record: {record_id}")
 
    if not image_urls:
        return jsonify({"error": "Aucune image"}), 400
    if not record_id:
        return jsonify({"error": "record_id manquant"}), 400
 
    # Filtrer les URLs vides
    image_urls = [u for u in image_urls if u and u.startswith("http")]
    logger.info(f"URLs valides: {len(image_urls)}")
 
    tmp_dir = tempfile.mkdtemp()
    results = {}
 
    try:
        logger.info("📥 Téléchargement des images...")
        downloaded = download_images(image_urls, tmp_dir)
        logger.info(f"✅ {len(downloaded)} images téléchargées")
 
        if len(downloaded) < 2:
            return jsonify({"error": f"Pas assez d'images: {len(downloaded)}"}), 400
 
        # 16:9 YouTube
        logger.info("🎬 Création timelapse 16:9...")
        out_16_9 = os.path.join(tmp_dir, "timelapse_16_9.mp4")
        if create_timelapse(tmp_dir, out_16_9, fps, 1920, 1080):
            upload_to_airtable(out_16_9, record_id, field_16_9)
            results["16_9"] = "✅"
        else:
            results["16_9"] = "❌"
 
        # 9:16 Reels
        logger.info("🎬 Création timelapse 9:16...")
        out_9_16 = os.path.join(tmp_dir, "timelapse_9_16.mp4")
        if create_timelapse(tmp_dir, out_9_16, fps, 1080, 1920):
            upload_to_airtable(out_9_16, record_id, field_9_16)
            results["9_16"] = "✅"
        else:
            results["9_16"] = "❌"
 
        logger.info(f"🏁 Terminé: {results}")
        return jsonify({"status": "success", "results": results}), 200
 
    except Exception as e:
        logger.error(f"❌ Erreur: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
 
 
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"}), 200
 
 
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
