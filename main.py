import os
import errno
import json
import sys
import threading
from pathlib import Path

import wave

import ffmpeg
import argostranslate.translate
import argostranslate.package
from PyQt5 import QtWidgets
from PyQt5.QtCore import QStandardPaths, QObject, pyqtSignal, QUrl, QSettings, Qt
from PyQt5.QtMultimedia import QMediaPlayer, QMediaContent
from PyQt5.QtMultimediaWidgets import QVideoWidget
from PyQt5.QtWidgets import QFileDialog, QSizePolicy, QPushButton
from fsspec.utils import seek_delimiter
from vosk import Model, KaldiRecognizer


class ProcessVideo:
    def __init__(self, video, srt_path, save_dir, model = "vosk-model-fr-0.22"):
        self.video = video
        self.model = model
        self.confidence = 0.6
        self._video_exits()
        self.video_dir = save_dir
        self.srt_path = srt_path


    def _video_exits(self):
        if not os.path.exists(self.video):
            raise FileNotFoundError(
                errno.ENOENT,
                os.strerror(errno.ENOENT),
                self.video
            )


    def _model_exists(self):
        if not os.path.exists("models/" + self.model):
            raise FileNotFoundError(
                errno.ENOENT,
                os.strerror(errno.ENOENT),
                self.model
            )


    def _format_timestamp(self, seconds):
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = seconds % 60
        return f"{hours:02d}:{minutes:02d}:{secs:06.3f}".replace('.', ',')


    def _save_srt(self, subtitles):
        srt_file = os.path.join(self.video_dir, f"{os.path.basename(self.video)[:-4]}.srt")
        with open(srt_file, "w", encoding = "utf-8") as f:
            for subtitle in subtitles:
                f.write(subtitle + "\n")
        return srt_file


    def _cleanup(self):
        base_name = os.path.basename(self.video)[:-4]
        srt_file = os.path.join(self.video_dir, f"{base_name}.srt")
        wav_file = os.path.join(self.video_dir, f"{base_name}.wav")
        if os.path.exists(srt_file):
            os.remove(srt_file)
        if os.path.exists(wav_file):
            os.remove(wav_file)


    def extract_audio(self):
        output_file = os.path.join(self.video_dir, f"{os.path.basename(self.video)[:-4]}.wav")
        (
            ffmpeg.input(
                self.video,
            ).output(output_file,
                    format = 'wav',
                    acodec = 'pcm_s16le',
                    ac = 1,
                    ar = '16000',
                    af = '''
                        highpass=f=200,
                        lowpass=f=3500,
                        afftdn=nf=-20,
                    ''',
            ).overwrite_output(
            ).run(
            )
        )


    def retrieve_text(self):
        model = Model("models/" + self.model)
        recognizer = KaldiRecognizer(model, 16000)
        recognizer.SetWords(True)
        subtitles = []
        subtitles_index = 1
        wav_file = os.path.join(self.video_dir, f"{os.path.basename(self.video)[:-4]}.wav")
        with wave.open(wav_file, 'rb') as wf:
            while True:
                data = wf.readframes(4000)
                if not data:
                    break
                if recognizer.AcceptWaveform(data):
                    result = json.loads(recognizer.Result())
                    if "result" in result:
                        words = result["result"]
                        if words:
                            start = self._format_timestamp(words[0]["start"])
                            end = self._format_timestamp(words[-1]["end"])
                            text = " ".join([word["word"] for word in words if (word["conf"] > self.confidence)])
                            subtitles.append(f"{subtitles_index}\n{start} --> {end}\n{text}\n")
                            subtitles_index += 1
        os.remove(wav_file)
        return self._save_srt(subtitles)


    def burn_subtitles(self):
        srt_path = self.srt_path
        output_video_path = self.video_dir
        (
            ffmpeg.input(
                self.video,
            ).output(output_video_path,
                    vf = f"subtitles={srt_path}",
                    vcodec = 'libx264',
                    acodec = 'copy'
            ).overwrite_output(
            ).run(
            )
        )
        self._cleanup()


class Translator:
    def __init__(self):
        pass


    def install_packages(self):
        argostranslate.package.update_package_index()
        available_packages = argostranslate.package.get_available_packages()
        packages = next(
            filter(
                lambda x: x.from_code == "fr" and x.to_code == "en", available_packages
            )
        )
        argostranslate.package.install_from_path(packages.download())


    def translate(self, text):
        translation = argostranslate.translate.translate(text, "fr", "en")
        return translation



class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        QtWidgets.QApplication.setOrganizationName("ryzedz")
        QtWidgets.QApplication.setApplicationName("TransFlix")
        self.settings = QSettings()
        self.video_path = None
        default_save_dir = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
        self.save_dir = self.settings.value("save_dir", default_save_dir)
        self.signals = Signals()
        self.initUI()
        center_window(self)
        self.signals.finished.connect(self.on_processing_finished)
        self.signals.error.connect(self.on_processing_error)


    def initUI(self):
        self.setWindowTitle("TransFlix")
        self.setGeometry(100, 100, 400, 200)
        layout = QtWidgets.QVBoxLayout()

        self.label_video = QtWidgets.QLabel("No video selected")
        self.button_video = QtWidgets.QPushButton("Select video: ")
        self.button_video.clicked.connect(self.select_video)
        layout.addWidget(self.label_video)
        layout.addWidget(self.button_video)

        self.label_folder = QtWidgets.QLabel(f"Save to: {self.save_dir}")
        self.button_folder = QtWidgets.QPushButton("Select save directory: ")
        self.button_folder.clicked.connect(self.select_folder)
        layout.addWidget(self.label_folder)
        layout.addWidget(self.button_folder)

        self.button_process = QtWidgets.QPushButton("Process Video")
        self.button_process.clicked.connect(self.start_processing)
        layout.addWidget(self.button_process)

        central_widget = QtWidgets.QWidget()
        central_widget.setLayout(layout)
        self.setCentralWidget(central_widget)


    def validate_inputs(self):
        if not self.video_path:
            QtWidgets.QMessageBox.warning(
                self,
                "Error",
                "Please select a video"
            )
            return False
        if not self.save_dir:
            QtWidgets.QMessageBox.warning(
                self,
                "Error",
                "Please select a save folder!"
            )
            return False
        return True


    def start_processing(self):
        if self.validate_inputs():
            self.button_process.setEnabled(False)
            self.progress_dialog = QtWidgets.QProgressDialog(
                "Processing",
                None,
                0,
                0,
                self
            )
            self.progress_dialog.show()
            thread = threading.Thread(
                target = self.run_processing,
                args = (self.video_path, self.save_dir),
                daemon = True
            )
            thread.start()


    def run_processing(self, video_path, save_dir):
        try:
            videoProcess = ProcessVideo(video_path, None, save_dir)
            videoProcess.extract_audio()
            srt_path = videoProcess.retrieve_text()
            print(srt_path)
            self.signals.finished.emit(srt_path)
        except Exception as e:
            self.signals.error.emit(str(e))


    def on_processing_finished(self, srt_path):
        self.progress_dialog.close()
        self.button_process.setEnabled(True)
        self.preview_window = PreviewWindow(self.video_path, srt_path, self.save_dir, self)
        self.preview_window.show()
        self.hide()


    def on_processing_error(self, error_msg):
        self.progress_dialog.close()
        self.button_process.setEnabled(True)
        QtWidgets.QMessageBox.critical(
            self,
            "Error",
            f"Processing failed:\n{error_msg}"
        )


    def select_video(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Video",
            "",
            "Video Files (*.mp4)"
        )
        if path:
            self.video_path = path
            self.label_video.setText(f"Selected: {path}")


    def select_folder(self):
        dir_path = QFileDialog.getExistingDirectory(
            self,
            "Select save directory",
            self.save_dir
        )
        if dir_path:
            self.save_dir = dir_path
            self.settings.setValue("save_dir", self.save_dir)
            self.label_folder.setText(f"Save to: {dir_path}")


class PreviewWindow(QtWidgets.QMainWindow):
    def __init__(self, video_path, srt_path, save_dir, main_window):
        super().__init__()
        self.video_path = video_path
        self.srt_path = srt_path
        self.save_dir = save_dir
        self.main_window = main_window
        self.signals = Signals()
        self.signals.burn_finished.connect(self.on_burn_finished)
        self.media_player = QMediaPlayer()
        self.initUI()
        self.showMaximized()


    def initUI(self):
        self.setWindowTitle("Preview")
        self.setGeometry(100, 100, 800, 600)
        layout = QtWidgets.QVBoxLayout()

        self.media_player = QMediaPlayer()
        self.video_widget = QVideoWidget()
        self.video_widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.media_player.setVideoOutput(self.video_widget)
        layout.addWidget(self.video_widget, stretch = 3)
        self.media_player.setMedia(QMediaContent(QUrl.fromLocalFile(self.video_path)))
        self.media_player.setVolume(50)
        self.media_player.positionChanged.connect(self.update_slider)
        self.media_player.play()

        self.sliders_layout = QtWidgets.QHBoxLayout()
        self.controls_layout = QtWidgets.QHBoxLayout()
        self.audio_layout = QtWidgets.QHBoxLayout()
        self.main_controls_layout = QtWidgets.QVBoxLayout()
        self.final_layout = QtWidgets.QVBoxLayout()

        self.button_backward = QtWidgets.QPushButton("←")
        self.button_backward.clicked.connect(self.seek_backward)
        self.controls_layout.addWidget(self.button_backward)

        self.button_play = QtWidgets.QPushButton("⏸")
        self.button_play.clicked.connect(self.toggle_play_pause)
        self.controls_layout.addWidget(self.button_play)

        self.button_forward = QtWidgets.QPushButton("→")
        self.button_forward.clicked.connect(self.seek_forward)
        self.controls_layout.addWidget(self.button_forward)

        self.seek_slider = QtWidgets.QSlider(Qt.Horizontal)
        self.seek_slider.setRange(0, 100)
        self.seek_slider.sliderMoved.connect(self.set_position)
        self.sliders_layout.addWidget(self.seek_slider, stretch = 10)

        self.separator = QtWidgets.QLabel("|")
        self.sliders_layout.addWidget(self.separator)

        self.button_mute = QtWidgets.QPushButton("🔊")
        self.button_mute.clicked.connect(self.toggle_mute)
        self.sliders_layout.addWidget(self.button_mute)

        self.volume_slider = QtWidgets.QSlider()
        self.volume_slider.setOrientation(Qt.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(50)
        self.volume_slider.valueChanged.connect(self.set_volume)
        self.sliders_layout.addWidget(self.volume_slider, stretch = 1)

        self.controls_layout.setAlignment(Qt.AlignCenter)
        self.main_controls_layout.addLayout(self.sliders_layout)
        self.main_controls_layout.addLayout(self.controls_layout)
        self.main_controls_layout.addLayout(self.audio_layout)

        self.final_layout.addLayout(self.main_controls_layout)
        layout.addLayout(self.final_layout)

        self.text_edit = QtWidgets.QTextEdit()
        self.text_edit.setFontPointSize(16)
        with open(self.srt_path, "r") as f:
            self.text_edit.setText(f.read())
        layout.addWidget(self.text_edit, stretch = 2)

        self.button_finish = QtWidgets.QPushButton("Finish and Burn")
        self.button_finish.clicked.connect(self.start_burning)
        layout.addWidget(self.button_finish)

        central_widget = QtWidgets.QWidget()
        central_widget.setLayout(layout)
        self.setCentralWidget(central_widget)


    def toggle_play_pause(self):
        if self.media_player.state() == QMediaPlayer.PlayingState:
            self.media_player.pause()
            self.button_play.setText("▶")
        else:
            self.media_player.play()
            self.button_play.setText("⏸")


    def seek_backward(self):
        current_position = self.media_player.position()
        self.media_player.setPosition(max(0, current_position - 5000))


    def seek_forward(self):
        current_position = self.media_player.position()
        duration = self.media_player.duration()
        self.media_player.setPosition(min(duration, current_position + 5000))


    def toggle_mute(self):
        is_muted = self.media_player.isMuted()
        self.media_player.setMuted(not is_muted)
        self.media_player.setVolume(50 if (not is_muted and self.media_player.volume() == 0) else self.media_player.volume())
        self.volume_slider.setValue(50 if (not is_muted and self.media_player.volume() == 0) else self.media_player.volume())
        self.button_mute.setText("🔇" if not is_muted else "🔊")


    def set_volume(self, value):
        self.media_player.setVolume(value)
        self.button_mute.setText("🔇" if value == 0 else "🔊")


    def update_slider(self, position):
        if self.media_player.duration() > 0:
            self.seek_slider.setValue(int((position / self.media_player.duration()) * 100))


    def set_position(self, value):
        self.media_player.setPosition(int((value / 100) * self.media_player.duration()))


    def start_burning(self):
        self.button_finish.setEnabled(False)
        self.media_player.stop()
        edited_srt = self.text_edit.toPlainText()
        translator = Translator()
        translated_srt = translator.translate(edited_srt)
        with open(self.srt_path, "w") as f:
            f.write(translated_srt)
        input_path = Path(self.video_path)
        output_file = f"{input_path.stem}_subtitled{input_path.suffix}"
        output_path = str(Path(self.save_dir) / output_file)
        self.burn_progress = QtWidgets.QProgressDialog(
            "Burning subtitles...",
            None,
            0,
            0,
            self
        )
        self.burn_progress.setWindowTitle("Processing")
        self.burn_progress.show()
        self.burn_thread = threading.Thread(
            target = self.run_burning,
            args = (output_path, ),
            daemon = True
        )
        self.burn_thread.start()


    def run_burning(self, output_path):
        try:
            videoprocess = ProcessVideo(
                self.video_path,
                self.srt_path,
                output_path
            )
            videoprocess.burn_subtitles()
            self.signals.burn_finished.emit(output_path, None)
        except Exception as e:
            self.signals.burn_finished.emit(None, str(e))


    def burn_subtitles(self, video_path, srt_path, output_path):
        try:
            videoProcess = ProcessVideo(video_path, srt_path, output_path)
            videoProcess.burn_subtitles()
            self.show_success(output_path)
        except Exception as e:
            QtWidgets.QMessageBox.critical(
                self,
                "Error",
                f"Burning failed:\n{str(e)}"
            )
        finally:
            self.progress.close()


    def on_burn_finished(self, output_path, error = None):
        self.burn_progress.close()
        self.button_finish.setEnabled(True)
        if error:
            QtWidgets.QMessageBox.critical(self, "Error", error)
        else:
            QtWidgets.QMessageBox.information(
                self,
                "Success",
                f"Subtitled video saved to: {output_path}"
            )
            self.close()
            self.main_window.show()
        os.remove(self.srt_path)


    def show_success(self, output_path):
        QtWidgets.QMessageBox.information(
            self,
            "Success",
            f"Subtitled video saved to:\n{output_path}"
        )
        self.close()
        self.main_window.show()


    def closeEvent(self, event):
        self.media_player.stop()
        self.media_player.setMedia(QMediaContent())
        event.accept()


class SuccessWindow(QtWidgets.QDialog):
    def __init__(self, output_path):
        super().__init__()
        self.output_path = output_path
        self.initUI()
        center_window(self)

    def initUI(self):
        self.setWindowTitle("Success")
        layout = QtWidgets.QVBoxLayout()

        label = QtWidgets.QLabel(f"Video created at:\n{self.output_path}")
        layout.addWidget(label)

        btn_ok = QtWidgets.QPushButton("OK")
        btn_ok.clicked.connect(self.return_to_main)
        layout.addWidget(btn_ok)

        self.setLayout(layout)

    def return_to_main(self):
        main_window = MainWindow()
        main_window.show()
        self.close()


class Signals(QObject):
    finished = pyqtSignal(str)
    error = pyqtSignal(str)
    burn_finished = pyqtSignal(str, str)


def center_window(window):
    screen = window.screen()
    screen_geometry = screen.availableGeometry()
    center = screen_geometry.center()
    window_geometry = window.frameGeometry()
    window_geometry.moveCenter(center)
    window.move(window_geometry.topLeft())


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())