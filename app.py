#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import re
import requests
import hashlib
import zipfile
import tempfile
import shutil
import logging
import base64
import subprocess
from io import BytesIO
from typing import List, Dict, Optional, Tuple
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template_string, session

# Configurar logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# -------------------- IMPORTACIONES DE DEPENDENCIAS --------------------
try:
    from spotify_scraper import SpotifyClient
    from mutagen.mp3 import MP3
    from mutagen.id3 import (
        ID3, APIC, TIT2, TPE1, TPE2, TALB, TDRC,
        TRCK, TCON, ID3NoHeaderError
    )
    from PIL import Image
except ImportError as e:
    logger.error(f"Falta dependencia: {e}")
    logger.error("Ejecuta: pip install -r requirements.txt")
    sys.exit(1)

# -------------------- CONFIGURACIÓN DE CARPETAS --------------------
DOWNLOAD_FOLDER = "descargas"
CARATULAS_TEMP = os.path.join(DOWNLOAD_FOLDER, "caratulas_temp")
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
os.makedirs(CARATULAS_TEMP, exist_ok=True)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # Límite 100MB
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')  # Necesario para sesiones

# -------------------- FUNCIONES DE UTILIDAD --------------------
def sanitizar_nombre(texto: str) -> str:
    """Elimina caracteres no válidos para nombres de archivo en Windows"""
    texto = texto.replace("\xa0", " ").strip()
    texto = re.sub(r'[<>:"/\\|?*]', '', texto)
    texto = texto.rstrip('. ')
    return texto or "sin_nombre"

def obtener_info_spotify(url: str) -> Tuple[str, Optional[str], List[Dict]]:
    """Obtiene metadatos de una URL de Spotify (playlist, álbum o track)"""
    logger.info(f"Extrayendo metadatos de: {url}")
    client = SpotifyClient()
    try:
        if "playlist" in url:
            data = client.get_playlist_info(url)
            nombre = data.get("name", "Playlist sin nombre")
            images = data.get("images", [])
            imagen_url = images[-1].get("url") if images else None
            items = data.get("tracks", [])
            canciones = [extraer_info_track(item.get("track", item)) for item in items]
        elif "album" in url:
            data = client.get_album_info(url)
            nombre = data.get("name", "Álbum sin nombre")
            images = data.get("images", [])
            imagen_url = images[0].get("url") if images else None
            tracks = data.get("tracks", [])
            canciones = [extraer_info_track(track) for track in tracks]
        elif "track" in url:
            data = client.get_track_info(url)
            nombre = data.get("name", "Canción sin nombre")
            canciones = [extraer_info_track(data)]
            imagen_url = canciones[0].get("cover_url") if canciones else None
        else:
            raise ValueError("URL no soportada. Debe ser de playlist, álbum o track.")
        return nombre, imagen_url, canciones
    finally:
        client.close()

def extraer_info_track(track: Dict) -> Dict:
    """Extrae campos relevantes de un track de Spotify"""
    nombre = track.get("name", "")
    artista = track.get("artists", [{}])[0].get("name", "")
    album = track.get("album", {}).get("name", "")
    year = None
    if track.get("album", {}).get("release_date"):
        year = track["album"]["release_date"][:4]
    track_number = track.get("track_number")
    cover_url = None
    images_album = track.get("album", {}).get("images", [])
    if images_album:
        cover_url = images_album[0].get("url")
    return {
        "nombre": nombre,
        "artista": artista,
        "album": album,
        "year": year,
        "track_number": track_number,
        "cover_url": cover_url,
        "id_spotify": track.get("id"),
        "genre": "Pop"  # Podría mejorarse obteniendo género del álbum si existe
    }

def obtener_caratula_bytes(cancion: Dict) -> Optional[bytes]:
    """Obtiene la carátula desde Spotify (o fallback a iTunes)"""
    if cancion.get("cover_url"):
        try:
            r = requests.get(cancion["cover_url"], timeout=10)
            if r.status_code == 200:
                return r.content
        except:
            pass
    # Fallback a iTunes (opcional, puedes desactivarlo si quieres)
    return None

def descargar_audio(consulta: str, intentos=2, cookie_content: Optional[str] = None) -> Optional[bytes]:
    """
    Descarga audio usando el binario estático de yt-dlp.
    El binario debe estar presente en la raíz del proyecto (./yt-dlp).
    """
    timestamp = int(time.time())
    temp_name = f"temp_{timestamp}"
    output_template = os.path.join(DOWNLOAD_FOLDER, f"{temp_name}.%(ext)s")
    
    # Ruta al binario de yt-dlp (se descarga durante el build)
    yt_dlp_bin = "./yt-dlp"
    if not os.path.exists(yt_dlp_bin):
        # Intentar en el PATH por si acaso
        yt_dlp_bin = "yt-dlp"
    
    # Preparar el comando base
    cmd = [
        yt_dlp_bin,
        "--format", "bestaudio/best",
        "--default-search", "ytsearch1",
        "--output", output_template,
        "--extract-audio",
        "--audio-format", "mp3",
        "--audio-quality", "192",
        "--quiet",
        "--retries", "3",
        "--fragment-retries", "3",
        "--skip-unavailable-fragments",
        # Forzar cliente android para evitar problemas
        "--extractor-args", "youtube:player_client=android,web",
        consulta
    ]
    
    # Si hay cookies, crear archivo temporal y agregar opción
    cookie_file_path = None
    if cookie_content and cookie_content.strip():
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as f:
                f.write(cookie_content)
                cookie_file_path = f.name
            cmd.extend(["--cookies", cookie_file_path])
            logger.info("🍪 Usando cookies proporcionadas por el usuario")
        except Exception as e:
            logger.error(f"Error al escribir archivo de cookies: {e}")
    
    for intento in range(1, intentos + 1):
        try:
            logger.info(f"Intento {intento}: usando yt-dlp binario con cliente android")
            # Ejecutar el comando
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            # Buscar el archivo MP3 generado
            mp3_path = os.path.join(DOWNLOAD_FOLDER, f"{temp_name}.mp3")
            if os.path.exists(mp3_path):
                with open(mp3_path, "rb") as f:
                    data = f.read()
                os.remove(mp3_path)
                return data
            else:
                logger.error("No se generó el archivo MP3")
                logger.error(f"Salida de yt-dlp: {result.stdout}")
                logger.error(f"Error de yt-dlp: {result.stderr}")
        except subprocess.CalledProcessError as e:
            logger.warning(f"⚠️ Intento {intento}/{intentos} falló: {e.stderr}")
            time.sleep(3)
        finally:
            if cookie_file_path and os.path.exists(cookie_file_path):
                try:
                    os.remove(cookie_file_path)
                except:
                    pass
    return None

def añadir_metadatos_bytes(mp3_bytes: bytes, meta: Dict, caratula_bytes: Optional[bytes]) -> bytes:
    """Añade metadatos ID3 al MP3 y devuelve los bytes modificados"""
    temp_path = os.path.join(DOWNLOAD_FOLDER, f"temp_{hashlib.md5(mp3_bytes).hexdigest()[:8]}.mp3")
    with open(temp_path, "wb") as f:
        f.write(mp3_bytes)

    try:
        audio = MP3(temp_path, ID3=ID3)
        try:
            audio.add_tags()
        except:
            pass
    except ID3NoHeaderError:
        audio = MP3(temp_path)
        audio.add_tags()
    if not audio.tags:
        audio.add_tags()

    # Eliminar carátulas existentes
    audio.tags.delall("APIC")
    
    # Añadir metadatos
    audio.tags["TIT2"] = TIT2(encoding=3, text=meta.get("nombre", "Desconocido"))
    audio.tags["TPE1"] = TPE1(encoding=3, text=meta.get("artista", "Desconocido"))
    audio.tags["TPE2"] = TPE2(encoding=3, text=meta.get("artista", "Desconocido"))
    audio.tags["TALB"] = TALB(encoding=3, text=meta.get("album", "Álbum Desconocido"))
    track_number = meta.get("track_number")
    audio.tags["TRCK"] = TRCK(encoding=3, text=str(track_number) if track_number else "1")
    if meta.get("year"):
        audio.tags["TDRC"] = TDRC(encoding=3, text=str(meta["year"]))
    audio.tags["TCON"] = TCON(encoding=3, text=meta.get("genre", "Pop"))

    if caratula_bytes:
        audio.tags.add(
            APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=caratula_bytes)
        )

    audio.save(v2_version=3)

    with open(temp_path, "rb") as f:
        modified = f.read()
    os.remove(temp_path)
    return modified

def crear_icono_y_desktop_ini(carpeta_destino: str, imagen_bytes: bytes) -> Tuple[Optional[str], Optional[str]]:
    """Crea archivo .ico y desktop.ini para personalizar carpeta en Windows"""
    try:
        img = Image.open(BytesIO(imagen_bytes))
        img = img.resize((256, 256), Image.Resampling.LANCZOS)
        ico_path = os.path.join(carpeta_destino, "cover.ico")
        img.save(ico_path, format='ICO', sizes=[(256, 256)])
        
        ini_path = os.path.join(carpeta_destino, "desktop.ini")
        contenido = f"""[.ShellClassInfo]
IconResource=cover.ico,0
[ViewState]
Mode=
Vid=
FolderType=Music
"""
        with open(ini_path, "w", encoding="utf-8") as f:
            f.write(contenido)
        
        # Ocultar archivos en Windows (si se ejecuta localmente)
        if os.name == 'nt':
            import ctypes
            ctypes.windll.kernel32.SetFileAttributesW(ico_path, 2)
            ctypes.windll.kernel32.SetFileAttributesW(ini_path, 2 | 4)
        
        return ico_path, ini_path
    except Exception as e:
        logger.warning(f"Error creando icono/desktop.ini: {e}")
        return None, None

# -------------------- RUTAS WEB --------------------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🎵 Spotify Downloader Web</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #1e1e2f, #2a2a40);
            color: #fff;
            min-height: 100vh;
            padding: 20px;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
        }
        h1 {
            text-align: center;
            margin-bottom: 10px;
            font-size: 2.5rem;
            background: linear-gradient(45deg, #1db954, #1ed760);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        .subtitle {
            text-align: center;
            margin-bottom: 30px;
            color: #aaa;
        }
        .input-group {
            display: flex;
            flex-direction: column;
            gap: 10px;
            max-width: 800px;
            margin: 0 auto 30px;
        }
        #url-input {
            width: 100%;
            padding: 15px 20px;
            border: none;
            border-radius: 50px;
            background: rgba(255,255,255,0.1);
            color: #fff;
            font-size: 16px;
            backdrop-filter: blur(5px);
            border: 1px solid rgba(255,255,255,0.2);
        }
        #url-input:focus {
            outline: 2px solid #1db954;
        }
        .cookie-section {
            display: flex;
            gap: 10px;
            align-items: center;
            flex-wrap: wrap;
        }
        #cookies-input {
            flex: 1;
            padding: 10px;
            border-radius: 10px;
            background: rgba(255,255,255,0.1);
            color: #fff;
            border: 1px solid rgba(255,255,255,0.2);
            font-family: monospace;
            resize: vertical;
        }
        button {
            padding: 15px 30px;
            border: none;
            border-radius: 50px;
            background: #1db954;
            color: #000;
            font-weight: bold;
            font-size: 16px;
            cursor: pointer;
            transition: transform 0.2s, background 0.2s;
            white-space: nowrap;
        }
        button:hover {
            background: #1ed760;
            transform: scale(1.05);
        }
        button:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        #fetch-btn {
            background: #1db954;
            margin-top: 10px;
        }
        #cookies-help {
            color: #aaa;
            font-size: 0.9rem;
            margin-top: 5px;
        }
        #cookies-help a {
            color: #1db954;
        }
        .loading {
            text-align: center;
            margin: 40px;
            font-size: 18px;
            color: #1db954;
        }
        .playlist-info {
            display: flex;
            align-items: center;
            gap: 20px;
            background: rgba(255,255,255,0.05);
            border-radius: 20px;
            padding: 20px;
            margin-bottom: 30px;
            backdrop-filter: blur(5px);
        }
        .playlist-cover {
            width: 100px;
            height: 100px;
            border-radius: 10px;
            object-fit: cover;
            box-shadow: 0 4px 15px rgba(0,0,0,0.5);
        }
        .playlist-name {
            font-size: 1.8rem;
            font-weight: bold;
        }
        .actions {
            display: flex;
            gap: 10px;
            margin-bottom: 20px;
            justify-content: flex-end;
        }
        .download-all-btn {
            background: #ff8c00;
        }
        .download-all-btn:hover {
            background: #ffa500;
        }
        .tracks-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
            gap: 20px;
            margin-top: 20px;
        }
        .track-card {
            background: rgba(255,255,255,0.05);
            border-radius: 15px;
            overflow: hidden;
            transition: transform 0.3s, box-shadow 0.3s;
            backdrop-filter: blur(5px);
            border: 1px solid rgba(255,255,255,0.1);
            display: flex;
            flex-direction: column;
        }
        .track-card:hover {
            transform: translateY(-5px);
            box-shadow: 0 10px 30px rgba(0,0,0,0.3);
        }
        .track-cover {
            width: 100%;
            aspect-ratio: 1/1;
            object-fit: cover;
            border-bottom: 2px solid #1db954;
        }
        .track-info {
            padding: 15px;
            flex: 1;
        }
        .track-name {
            font-weight: bold;
            font-size: 1.1rem;
            margin-bottom: 5px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .track-artist {
            color: #aaa;
            font-size: 0.9rem;
            margin-bottom: 5px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .track-album {
            color: #888;
            font-size: 0.8rem;
            margin-bottom: 10px;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }
        .track-year {
            color: #1db954;
            font-size: 0.8rem;
            margin-bottom: 10px;
        }
        .download-btn {
            width: 100%;
            padding: 10px;
            background: #1db954;
            color: #000;
            border: none;
            font-weight: bold;
            cursor: pointer;
            transition: background 0.2s;
            margin-top: auto;
        }
        .download-btn:hover {
            background: #1ed760;
        }
        .download-btn:disabled {
            background: #555;
            cursor: wait;
        }
        .error {
            color: #ff6b6b;
            text-align: center;
            margin: 20px;
        }
        footer {
            text-align: center;
            margin-top: 50px;
            color: #666;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🎵 Spotify Downloader Web</h1>
        <div class="subtitle">Descarga canciones, álbumes o playlists con un solo clic</div>
        
        <div class="input-group">
            <input type="text" id="url-input" placeholder="Pega la URL de Spotify (canción, álbum o playlist)..." value="">
            
            <div class="cookie-section">
                <textarea id="cookies-input" placeholder="Opcional: pega aquí el contenido de tu archivo cookies.txt (para evitar bloqueos de YouTube)" rows="3"></textarea>
            </div>
            <div id="cookies-help">
                📌 ¿Necesitas cookies? Sigue <a href="https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies" target="_blank">esta guía</a> para exportar tu archivo cookies.txt (usa ventana de incógnito) y pégalo arriba. Las cookies solo se usan temporalmente y se eliminan tras la descarga.
            </div>
            
            <button id="fetch-btn">Obtener canciones</button>
        </div>
        
        <div id="loading" class="loading" style="display: none;">⏳ Cargando información...</div>
        <div id="error" class="error" style="display: none;"></div>
        
        <div id="playlist-container" style="display: none;">
            <div class="playlist-info" id="playlist-info">
                <img id="playlist-cover" class="playlist-cover" src="" alt="Cover">
                <div>
                    <div class="playlist-name" id="playlist-name"></div>
                    <div id="playlist-stats"></div>
                </div>
            </div>
            
            <div class="actions">
                <button id="download-all-btn" class="download-all-btn">📦 Descargar todo en ZIP</button>
            </div>
            
            <div id="tracks-container" class="tracks-grid"></div>
        </div>
        
        <footer>Hecho con ❤️ usando SpotifyScraper y yt-dlp</footer>
    </div>

    <script>
        let currentTracks = [];
        let currentPlaylistName = '';
        let currentPlaylistCover = '';
        
        document.getElementById('fetch-btn').addEventListener('click', async () => {
            const url = document.getElementById('url-input').value.trim();
            if (!url) {
                alert('Por favor, ingresa una URL de Spotify');
                return;
            }
            
            const cookies = document.getElementById('cookies-input').value.trim();
            
            document.getElementById('loading').style.display = 'block';
            document.getElementById('error').style.display = 'none';
            document.getElementById('playlist-container').style.display = 'none';
            document.getElementById('fetch-btn').disabled = true;
            
            try {
                const response = await fetch('/get_tracks', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url, cookies })
                });
                
                const data = await response.json();
                
                if (!response.ok) {
                    throw new Error(data.error || 'Error desconocido');
                }
                
                currentPlaylistName = data.name;
                currentPlaylistCover = data.cover_base64 || '';
                currentTracks = data.tracks;
                
                document.getElementById('playlist-name').textContent = data.name;
                document.getElementById('playlist-cover').src = data.cover_base64 || 'https://via.placeholder.com/100?text=No+Cover';
                document.getElementById('playlist-stats').textContent = `${data.tracks.length} canciones`;
                
                const tracksContainer = document.getElementById('tracks-container');
                tracksContainer.innerHTML = '';
                
                data.tracks.forEach((track, index) => {
                    const card = document.createElement('div');
                    card.className = 'track-card';
                    
                    const coverImg = track.cover_base64 || 'https://via.placeholder.com/220?text=No+Cover';
                    
                    card.innerHTML = `
                        <img class="track-cover" src="${coverImg}" alt="Cover" loading="lazy">
                        <div class="track-info">
                            <div class="track-name" title="${track.nombre}">${track.nombre}</div>
                            <div class="track-artist" title="${track.artista}">${track.artista}</div>
                            <div class="track-album" title="${track.album}">${track.album}</div>
                            <div class="track-year">${track.year || 'Año desconocido'}</div>
                        </div>
                        <button class="download-btn" data-index="${index}">⬇️ Descargar MP3</button>
                    `;
                    
                    tracksContainer.appendChild(card);
                });
                
                document.querySelectorAll('.download-btn').forEach(btn => {
                    btn.addEventListener('click', async (e) => {
                        const index = e.target.dataset.index;
                        await downloadTrack(index, e.target);
                    });
                });
                
                document.getElementById('playlist-container').style.display = 'block';
            } catch (err) {
                document.getElementById('error').textContent = 'Error: ' + err.message;
                document.getElementById('error').style.display = 'block';
            } finally {
                document.getElementById('loading').style.display = 'none';
                document.getElementById('fetch-btn').disabled = false;
            }
        });
        
        document.getElementById('download-all-btn').addEventListener('click', async () => {
            if (!currentTracks.length) return;
            
            const btn = document.getElementById('download-all-btn');
            btn.disabled = true;
            btn.textContent = '⏳ Preparando ZIP...';
            
            const cookies = document.getElementById('cookies-input').value.trim();
            
            try {
                const response = await fetch('/download_all', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ 
                        tracks: currentTracks,
                        playlist_name: currentPlaylistName,
                        playlist_cover: currentPlaylistCover,
                        cookies: cookies
                    })
                });
                
                if (!response.ok) {
                    throw new Error('Error al generar el ZIP');
                }
                
                const blob = await response.blob();
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `${currentPlaylistName.replace(/[<>:"/\\|?*]/g, '_')}.zip`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                window.URL.revokeObjectURL(url);
                
                btn.textContent = '✅ ZIP listo';
                setTimeout(() => {
                    btn.textContent = '📦 Descargar todo en ZIP';
                    btn.disabled = false;
                }, 3000);
            } catch (err) {
                alert('Error: ' + err.message);
                btn.textContent = '📦 Descargar todo en ZIP';
                btn.disabled = false;
            }
        });
        
        async function downloadTrack(index, button) {
            const track = currentTracks[index];
            if (!track) return;
            
            const cookies = document.getElementById('cookies-input').value.trim();
            
            button.disabled = true;
            button.textContent = '⏳ Descargando...';
            
            try {
                const response = await fetch('/download_track', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ track, cookies })
                });
                
                if (!response.ok) {
                    throw new Error('Error en la descarga');
                }
                
                const blob = await response.blob();
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `${track.artista} - ${track.nombre}.mp3`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                window.URL.revokeObjectURL(url);
                
                button.textContent = '✅ Descargado';
                setTimeout(() => {
                    button.textContent = '⬇️ Descargar MP3';
                    button.disabled = false;
                }, 2000);
            } catch (err) {
                alert('Error al descargar: ' + err.message);
                button.textContent = '⬇️ Descargar MP3';
                button.disabled = false;
            }
        }
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    logger.info("Solicitud a /")
    return render_template_string(HTML_TEMPLATE)

@app.route('/get_tracks', methods=['POST'])
def api_get_tracks():
    data = request.get_json()
    url = data.get('url')
    cookies = data.get('cookies', '')
    if not url:
        return jsonify({'error': 'URL requerida'}), 400
    
    try:
        # Guardar cookies en la sesión (opcional, pero podemos usarlas directamente en cada descarga)
        if cookies:
            session['youtube_cookies'] = cookies
        
        nombre, imagen_url, canciones = obtener_info_spotify(url)
        cover_base64 = None
        if imagen_url:
            try:
                r = requests.get(imagen_url, timeout=10)
                if r.status_code == 200:
                    encoded = base64.b64encode(r.content).decode('utf-8')
                    cover_base64 = f"data:image/jpeg;base64,{encoded}"
            except:
                pass
        
        for cancion in canciones:
            if cancion.get('cover_url'):
                try:
                    r = requests.get(cancion['cover_url'], timeout=5)
                    if r.status_code == 200:
                        encoded = base64.b64encode(r.content).decode('utf-8')
                        cancion['cover_base64'] = f"data:image/jpeg;base64,{encoded}"
                except:
                    cancion['cover_base64'] = None
            else:
                cancion['cover_base64'] = None
        
        return jsonify({
            'name': nombre,
            'cover_base64': cover_base64,
            'tracks': canciones
        })
    except Exception as e:
        logger.error(f"Error en /get_tracks: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/download_track', methods=['POST'])
def api_download_track():
    data = request.get_json()
    track = data.get('track')
    cookies = data.get('cookies', session.get('youtube_cookies', ''))
    if not track:
        return jsonify({'error': 'Track requerido'}), 400
    
    try:
        consulta = f"{track['nombre']} {track['artista']} audio"
        mp3_bytes = descargar_audio(consulta, cookie_content=cookies)
        if not mp3_bytes:
            return jsonify({'error': 'No se pudo descargar el audio'}), 500
        
        caratula_bytes = obtener_caratula_bytes(track)
        mp3_con_metadatos = añadir_metadatos_bytes(mp3_bytes, track, caratula_bytes)
        
        return send_file(
            BytesIO(mp3_con_metadatos),
            as_attachment=True,
            download_name=f"{track['artista']} - {track['nombre']}.mp3",
            mimetype='audio/mpeg'
        )
    except Exception as e:
        logger.error(f"Error en /download_track: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/download_all', methods=['POST'])
def api_download_all():
    data = request.get_json()
    tracks = data.get('tracks', [])
    playlist_name = data.get('playlist_name', 'descarga')
    playlist_cover_b64 = data.get('playlist_cover', '')
    cookies = data.get('cookies', session.get('youtube_cookies', ''))
    
    if not tracks:
        return jsonify({'error': 'No hay canciones'}), 400
    
    # Crear carpeta temporal para el ZIP
    temp_dir = os.path.join(DOWNLOAD_FOLDER, f"temp_{int(time.time())}")
    os.makedirs(temp_dir, exist_ok=True)
    
    zip_path = os.path.join(DOWNLOAD_FOLDER, f"{sanitizar_nombre(playlist_name)}.zip")
    
    try:
        # Guardar imagen de la playlist si existe
        playlist_cover_bytes = None
        if playlist_cover_b64 and playlist_cover_b64.startswith('data:image'):
            header, encoded = playlist_cover_b64.split(',', 1)
            playlist_cover_bytes = base64.b64decode(encoded)
            cover_path = os.path.join(temp_dir, "cover.jpg")
            with open(cover_path, "wb") as f:
                f.write(playlist_cover_bytes)
        
        # Descargar cada canción
        for i, track in enumerate(tracks, 1):
            logger.info(f"Procesando {i}/{len(tracks)}: {track['nombre']}")
            consulta = f"{track['nombre']} {track['artista']} audio"
            mp3_bytes = descargar_audio(consulta, cookie_content=cookies)
            if not mp3_bytes:
                logger.warning(f"   ⚠️ Error descargando {track['nombre']}, se omite")
                continue
            
            caratula_bytes = obtener_caratula_bytes(track)
            mp3_con_metadatos = añadir_metadatos_bytes(mp3_bytes, track, caratula_bytes)
            
            filename = f"{track['artista']} - {track['nombre']}.mp3"
            filepath = os.path.join(temp_dir, sanitizar_nombre(filename))
            with open(filepath, "wb") as f:
                f.write(mp3_con_metadatos)
        
        # Crear icono y desktop.ini si tenemos imagen de la playlist
        if playlist_cover_bytes:
            crear_icono_y_desktop_ini(temp_dir, playlist_cover_bytes)
        
        # Crear ZIP
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(temp_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, temp_dir)
                    zipf.write(file_path, arcname)
        
        # Enviar ZIP
        return send_file(
            zip_path,
            as_attachment=True,
            download_name=f"{sanitizar_nombre(playlist_name)}.zip",
            mimetype='application/zip'
        )
    except Exception as e:
        logger.error(f"Error en /download_all: {e}")
        return jsonify({'error': str(e)}), 500
    finally:
        # Limpiar archivos temporales
        shutil.rmtree(temp_dir, ignore_errors=True)
        if os.path.exists(zip_path):
            os.remove(zip_path)

# -------------------- MAIN --------------------
if __name__ == '__main__':
    # En producción, usar gunicorn
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)