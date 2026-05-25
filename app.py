from flask import Flask, request, jsonify
import subprocess
import os
import requests
import tempfile
import shutil
from pathlib import Path

app = Flask(__name__)

# ============================================================
# CONFIGURATION — modifie ces valeurs
# ============================================================
AIRTABLE_TOKEN = os.environ.get("AIRTABLE_TOKEN", "")       # Token Airtable
AIRTABLE_BASE_ID = os.environ.get("AIRTABLE_BASE_ID", "")   # ID de ta base Airtable
AIRTABLE_TABLE_NAME = os.environ.get("AIRTABLE_TABLE_NAME", "Timelapse")  # Nom de ta table
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "mon_secret_123")       # Clé secrète Make.com
# ============================================================


def download_images(image_urls: list, folder: str) -> list:
    """Télécharge les images depuis les URLs et les sauvegarde dans un dossier."""
    paths = []
    for i, url in enumerate(image_urls):
        ext = ".jpg"
        filename = os.path.join(folder, f"frame_{i:05d}{ext}")
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        with open(filename, "wb") as f:
            f.write(response.content)
        paths.append(filename)
        print(f"  ✅ Image {i+1}/{len(image_urls)} téléchargée")
    return paths


def create_timelapse(input_folder: str, output_path: str, fps: int, width: int, height: int) -> bool:
    """Assemble les images en vidéo timelapse avec FFmpeg."""
    input_pattern = os.path.join(input_folder, "frame_%05d.jpg")
    
    # Filtre pour redimensionner + remplir avec du noir si besoin (letterbox/pillarbox)
    vf_filter = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black"
    )

    cmd = [
        "ffmpeg", "-y",
        "-framerate", str(fps),
        "-i", input_pattern,
        "-vf", vf_filter,
        "-c:v", "libx264",
        "-crf", "18",               # Qualité (0=parfait, 51=mauvais, 18=excellent)
        "-preset", "fast",
        "-pix_fmt", "yuv420p",      # Compatible partout (YouTube, Instagram, TikTok)
        output_path
    ]

    print(f"  🎬 FFmpeg : {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"  ❌ Erreur FFmpeg : {result.stderr}")
        return False

    print(f"  ✅ Vidéo créée : {output_path}")
    return True


def upload_to_airtable(video_path: str, record_id: str, field_name: str) -> bool:
    """Upload la vidéo dans un champ Airtable."""
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_NAME}/{record_id}"
    headers = {
        "Authorization": f"Bearer {AIRTABLE_TOKEN}",
        "Content-Type": "application/json"
    }

    # Airtable nécessite une URL publique pour les pièces jointes
    # On upload d'abord sur un service temporaire (tmpfiles.org)
    with open(video_path, "rb") as f:
        upload_response = requests.post(
            "https://tmpfiles.org/api/v1/upload",
            files={"file": f}
        )

    if upload_response.status_code != 200:
        print(f"  ❌ Erreur upload temporaire : {upload_response.text}")
        return False

    # Convertir l'URL tmpfiles en URL directe
    tmp_url = upload_response.json()["data"]["url"].replace(
        "tmpfiles.org/", "tmpfiles.org/dl/"
    )
    print(f"  📤 URL temporaire : {tmp_url}")

    # Envoyer l'URL dans Airtable
    payload = {
        "fields": {
            field_name: [{"url": tmp_url}]
        }
    }

    response = requests.patch(url, headers=headers, json=payload)
    if response.status_code == 200:
        print(f"  ✅ Vidéo envoyée dans Airtable (champ : {field_name})")
        return True
    else:
        print(f"  ❌ Erreur Airtable : {response.text}")
        return False


# ============================================================
# ENDPOINT PRINCIPAL — appelé par Make.com
# ============================================================
@app.route("/create-timelapse", methods=["POST"])
def create_timelapse_endpoint():
    """
    Make.com envoie un JSON avec :
    {
        "secret": "mon_secret_123",
        "record_id": "recXXXXXXXX",        ← ID du record Airtable
        "images": ["url1", "url2", ...],    ← URLs des photos
        "fps": 24,                          ← Vitesse (images/seconde)
        "airtable_field_16_9": "Video_YouTube",
        "airtable_field_9_16": "Video_Reels"
    }
    """

    data = request.get_json()

    # Vérification de la clé secrète
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Non autorisé"}), 401

    image_urls = data.get("images", [])
    record_id = data.get("record_id", "")
    fps = int(data.get("fps", 24))
    field_16_9 = data.get("airtable_field_16_9", "Video_YouTube")
    field_9_16 = data.get("airtable_field_9_16", "Video_Reels")

    if not image_urls:
        return jsonify({"error": "Aucune image fournie"}), 400

    if not record_id:
        return jsonify({"error": "record_id manquant"}), 400

    print(f"\n🚀 Timelapse démarré — {len(image_urls)} images, {fps} fps")

    # Dossier temporaire pour les images et vidéos
    tmp_dir = tempfile.mkdtemp()

    try:
        # 1. Téléchargement des images
        print("\n📥 Téléchargement des images...")
        download_images(image_urls, tmp_dir)

        results = {}

        # 2. Création timelapse 16:9 (YouTube — 1920x1080)
        print("\n🎬 Création timelapse 16:9 (YouTube)...")
        output_16_9 = os.path.join(tmp_dir, "timelapse_16_9.mp4")
        ok_16_9 = create_timelapse(tmp_dir, output_16_9, fps, 1920, 1080)
        if ok_16_9:
            upload_to_airtable(output_16_9, record_id, field_16_9)
            results["16_9"] = "✅ OK"
        else:
            results["16_9"] = "❌ Erreur"

        # 3. Création timelapse 9:16 (Instagram Reels / TikTok — 1080x1920)
        print("\n🎬 Création timelapse 9:16 (Reels/TikTok)...")
        output_9_16 = os.path.join(tmp_dir, "timelapse_9_16.mp4")
        ok_9_16 = create_timelapse(tmp_dir, output_9_16, fps, 1080, 1920)
        if ok_9_16:
            upload_to_airtable(output_9_16, record_id, field_9_16)
            results["9_16"] = "✅ OK"
        else:
            results["9_16"] = "❌ Erreur"

        print(f"\n✅ Terminé ! Résultats : {results}")
        return jsonify({"status": "success", "results": results}), 200

    except Exception as e:
        print(f"\n❌ Erreur inattendue : {str(e)}")
        return jsonify({"error": str(e)}), 500

    finally:
        # Nettoyage du dossier temporaire
        shutil.rmtree(tmp_dir, ignore_errors=True)


@app.route("/health", methods=["GET"])
def health():
    """Endpoint pour vérifier que le serveur tourne."""
    return jsonify({"status": "ok", "message": "Serveur timelapse opérationnel ✅"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
