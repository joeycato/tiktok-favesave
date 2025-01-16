from json import load as json_load
from os import makedirs, listdir
from os import path
import sys
from pathlib import Path  # Added for improved path handling

import yt_dlp
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QCoreApplication
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSpacerItem,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QLabel,
)

# Function to load JSON file
def load_json(json_file):
    with open(json_file, 'r') as file:
        return json_load(file)


# Function to download video using yt-dlp
def download_video(video_url, download_folder, prefix):
    ydl_opts = {
        # Output template for downloaded videos
        'outtmpl': path.join(download_folder, f"{prefix}%(id)s.%(ext)s"),
        # Specify the format to download: best available video and audio
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best',
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([video_url])


# Function to check if a video is already downloaded
def is_video_downloaded(log_callback, video_url, downloaded_videos, prefix):
    # Extract video_id from URL (assuming the last part of the path is the video id)
    video_id = video_url.strip('/').split('/')[-1]
    expected_filenames = [
        file for file in downloaded_videos
        if file.startswith(prefix)
        and (file.endswith(f"{video_id}.mp4")
             or file.endswith(f"{video_id}.m4a")
             or file.endswith(f"{video_id}.mp3"))
    ]
    return len(expected_filenames) > 0


# Function to get a set of already downloaded video filenames; creates folder if needed
def get_downloaded_videos(download_folder):
    downloaded_videos = set()
    # Use pathlib for more robust path handling
    folder = Path(download_folder)
    folder.mkdir(parents=True, exist_ok=True)
    for file_path in folder.iterdir():
        if file_path.is_file():
            downloaded_videos.add(file_path.name)
    return downloaded_videos


# Main processing function (with progress callback added)
def process_videos(json_file, download_folder, log_callback, progress_callback, download_faves, download_likes):
    # Attempt to load the JSON file
    try:
        data = load_json(json_file)
    except Exception as e:
        log_callback(f"Error loading JSON file: {e}")
        return 0, 0, 0, 0, 0, 0, []  # Return zero counts on error

    video_links = []
    faves_count = 0
    likes_count = 0

    # Process favorite videos if selected
    if download_faves:
        favorite_videos = data.get('Activity', {}).get('Favorite Videos', {}).get('FavoriteVideoList', [])
        faves_count = len(favorite_videos)
        for video in favorite_videos:
            # Format date for filename prefix
            date = video.get('Date', '').replace(':', '').replace(' ', '-').replace('/', '-')
            video_links.append((video['Link'], f"faved_{date}_" if date else "faved_"))

    # Process liked videos if selected
    if download_likes:
        liked_videos = data.get('Activity', {}).get('Like List', {}).get('ItemFavoriteList', [])
        likes_count = len(liked_videos)
        for video in liked_videos:
            # Format date for filename prefix
            date = video.get('date', '').replace(':', '').replace(' ', '-').replace('/', '-')
            video_links.append((video['link'], f"liked_{date}_" if date else "liked_"))

    downloaded_videos = get_downloaded_videos(download_folder)

    total_videos = len(video_links)
    if total_videos == 0:
        log_callback("No videos to download.")
        return 0, 0, 0, 0, 0, 0, []

    downloaded_count = 0
    skipped_count = 0
    downloaded_faves = 0
    downloaded_likes = 0
    skipped_faves = 0
    skipped_likes = 0

    # Process each video in the list
    for index, (url, prefix) in enumerate(video_links, start=1):
        log_callback(f"🎥 Processing Video {index} of {total_videos}")

        if is_video_downloaded(log_callback, url, downloaded_videos, prefix):
            log_callback(f"Skipping (already downloaded): {url}")
            skipped_count += 1
            if "faved_" in prefix:
                skipped_faves += 1
            else:
                skipped_likes += 1
        else:
            log_callback(f"Downloading: {url}")
            try:
                download_video(url, download_folder, prefix)
                downloaded_count += 1
                if "faved_" in prefix:
                    downloaded_faves += 1
                else:
                    downloaded_likes += 1
            except Exception as e:
                log_callback(f"Failed to download {url}: {e}")

        # Update progress
        progress = int((index / total_videos) * 100)
        progress_callback(progress)
        QCoreApplication.processEvents()

    return (
        total_videos,
        downloaded_count,
        skipped_count,
        downloaded_faves,
        downloaded_likes,
        skipped_faves,
        skipped_likes,
        video_links
    )


# Worker Thread to handle video processing in the background
class VideoDownloadWorker(QThread):
    log_signal = pyqtSignal(str)
    progress_signal = pyqtSignal(int)

    def __init__(self, json_file, download_folder, download_faves, download_likes):
        super().__init__()
        self.json_file = json_file
        self.download_folder = download_folder
        self.download_faves = download_faves
        self.download_likes = download_likes
        self.total_videos = 0
        self.downloaded_videos = 0
        self.skipped_videos = 0
        self.downloaded_faves = 0
        self.downloaded_likes = 0
        self.skipped_faves = 0
        self.skipped_likes = 0

    def run(self):
        results = process_videos(
            self.json_file,
            self.download_folder,
            self.log_signal.emit,
            self.progress_signal.emit,
            self.download_faves,
            self.download_likes
        )
        (
            self.total_videos,
            self.downloaded_videos,
            self.skipped_videos,
            self.downloaded_faves,
            self.downloaded_likes,
            self.skipped_faves,
            self.skipped_likes,
            _
        ) = results


# PyQt6 Main Window for the Video Downloader Application
class VideoDownloaderApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FaveSave - TikTok Video Downloader")
        self.setGeometry(100, 100, 600, 500)

        self.json_file = None
        self.download_folder = ""
        self.worker = None
        self.progress_bar = None

        # Initialize UI components
        self.init_ui()

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)

        # Title label
        title_label = QLabel("Download Your Favorite TikTok Videos")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_label.setStyleSheet("font-size: 24px; font-weight: bold;")
        layout.addWidget(title_label)

        # JSON Path label with instructions and link
        self.json_label = QLabel("1. Select your JSON file from your exported TikTok data ( Click <a href='https://www.favesave.net'>here</a> for more details ):")
        self.json_label.setOpenExternalLinks(True)
        layout.addWidget(self.json_label)

        # Button to select JSON file
        self.json_button = QPushButton("No JSON file selected ( Click here to add...)")
        self.json_button.clicked.connect(self.set_json_path)
        self.json_button.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.json_button)

        # Output Folder label and button
        self.output_folder_label = QLabel("2. Set Download Folder ( where you want to save your videos ):")
        layout.addWidget(self.output_folder_label)

        self.output_folder_button = QPushButton("( Defaults to parent directory of selected JSON file above )")
        self.output_folder_button.clicked.connect(self.set_output_folder)
        self.output_folder_button.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.output_folder_button)

        # Label for download options
        download_options_label = QLabel("3. Which videos do you wish to download?")
        download_options_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(download_options_label)

        # Checkbox for favorite videos
        self.faves_checkbox = QCheckBox("🔖 Favorites")
        self.faves_checkbox.setChecked(True)
        layout.addWidget(self.faves_checkbox)

        # Checkbox for liked videos
        self.likes_checkbox = QCheckBox("❤️ Liked")
        self.likes_checkbox.setChecked(True)
        layout.addWidget(self.likes_checkbox)

        # Text area for logging messages
        self.description = QTextEdit()
        self.description.setPlaceholderText("Logs will appear here...")
        self.description.setReadOnly(True)
        layout.addWidget(self.description)

        # Progress bar for download progress
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        # Spacer and start button for download
        spacer = QSpacerItem(0, 20, QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Expanding)
        layout.addItem(spacer)
        self.start_button = QPushButton("4. Start Download")
        self.start_button.clicked.connect(self.start_download)
        self.start_button.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(self.start_button)

        # Donation link label
        donation_label = QLabel('☕️ <a href="https://buymeacoffee.com/myretrotvs">Buy me a coffee</a>')
        donation_label.setOpenExternalLinks(True)
        donation_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(donation_label)

    # Append message to the log area
    def log_message(self, message):
        self.description.append(message)
        self.description.ensureCursorVisible()

    # Update the checkbox labels with counts from the JSON file
    def update_checkbox_labels(self):
        if self.json_file:
            try:
                data = load_json(self.json_file)
                favorite_videos = data.get('Activity', {}).get('Favorite Videos', {}).get('FavoriteVideoList', [])
                liked_videos = data.get('Activity', {}).get('Like List', {}).get('ItemFavoriteList', [])
                self.faves_checkbox.setText(f"🔖 Favorites ({len(favorite_videos)} available)")
                self.likes_checkbox.setText(f"❤️ Liked ({len(liked_videos)} available)")
            except Exception as e:
                self.log_message(f"Error loading JSON file for video count: {e}")

    # Open a file dialog to select the JSON file
    def set_json_path(self):
        json_file, _ = QFileDialog.getOpenFileName(
            self, "Select JSON File", "", "JSON Files (*.json);;All Files (*)"
        )
        if json_file:
            self.json_file = json_file
            self.update_checkbox_labels()
            self.json_button.setText(f"JSON Path: {json_file}")
            self.log_message(f"JSON path set to: {json_file}")

            # Update download_folder to the parent directory of the JSON file
            if not self.download_folder:
                self.download_folder = str(Path(json_file).parent / "downloaded_videos")
            self.output_folder_label.setText("Set Output Folder:")
            self.output_folder_button.setText(self.download_folder)
            self.log_message(f"Output folder set to: {self.download_folder}")

    # Open a file dialog to select the output folder
    def set_output_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Output Folder", "")
        if folder:
            self.download_folder = folder
            self.output_folder_label.setText("Set Output Folder:")
            self.output_folder_button.setText(folder)
            self.log_message(f"Output folder set to: {folder}")

    # Start the download process
    def start_download(self):
        if not self.json_file:
            QMessageBox.warning(self, "Warning", "Please set a JSON path first.")
            return

        download_faves = self.faves_checkbox.isChecked()
        download_likes = self.likes_checkbox.isChecked()

        self.log_message(f"Selected JSON File: {self.json_file}")
        self.log_message(f"Selected Output Folder: {self.download_folder}")

        # Reset the progress bar to zero
        self.progress_bar.setValue(0)

        # Create a worker thread to process downloads without freezing the UI
        self.worker = VideoDownloadWorker(self.json_file, self.download_folder, download_faves, download_likes)
        self.worker.log_signal.connect(self.log_message)
        self.worker.progress_signal.connect(self.update_progress_bar)
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.start()

    # Called when the worker thread finishes processing
    def on_worker_finished(self):
        self.log_message(f"🍿 {self.worker.total_videos} total videos processed!")
        self.log_message(f"🔖 Favorite Videos: Downloaded: {self.worker.downloaded_faves}, Skipped: {self.worker.skipped_faves}")
        self.log_message(f"❤️ Liked Videos: Downloaded: {self.worker.downloaded_likes}, Skipped: {self.worker.skipped_likes}")
        self.start_button.setText("Done! (Click again to restart)")
        self.worker = None

    # Update the progress bar value
    def update_progress_bar(self, value):
        self.progress_bar.setValue(value)


# Run the application
if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = VideoDownloaderApp()
    window.show()
    sys.exit(app.exec())
