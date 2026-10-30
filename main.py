import ctypes
import functools
import logging
import queue
from ctypes import c_int, c_uint, c_void_p, c_wchar_p
from concurrent.futures import ThreadPoolExecutor

import comtypes
import customtkinter as ctk
import wmi
from comtypes import CLSCTX_ALL, COMMETHOD, GUID, HRESULT, IUnknown, CoCreateInstance
from monitorcontrol import get_monitors
from pycaw.pycaw import AudioUtilities

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pc-control")

WARNING_COLOR = "#fbbf24"
SUCCESS_COLOR = "#34d399"
MUTED_COLOR = "#94a3b8"

SPI_GETMOUSESPEED = 0x0070
SPI_SETMOUSESPEED = 0x0071
SPIF_UPDATEINIFILE = 0x0001
SPIF_SENDCHANGE = 0x0002
MIN_POINTER_SPEED = 1
MAX_POINTER_SPEED = 20

VK_MEDIA_NEXT_TRACK = 0xB0
VK_MEDIA_PREV_TRACK = 0xB1
VK_MEDIA_PLAY_PAUSE = 0xB3
KEYEVENTF_KEYUP = 0x0002

E_RENDER = 0
E_MULTIMEDIA = 1
DEVICE_ROLES = (0, 1, 2)  # console, multimedia, communications

POLICY_CONFIG_CLSID = "{294935CE-F637-4E7C-A41B-AB255460B862}"
POLICY_CONFIG_IID = "{568B9108-44BF-40B4-9006-86AFE5B5A620}"
MONITOR_SETTERS = {"brightness": "set_luminance", "contrast": "set_contrast"}


def _reserved_method(name):
    return COMMETHOD([], HRESULT, name, (["in"], c_void_p, "reserved"))


class IPolicyConfigVista(IUnknown):
    """Недокументированный интерфейс Windows для смены устройства вывода по умолчанию."""

    _iid_ = GUID(POLICY_CONFIG_IID)
    _methods_ = [
        _reserved_method("GetMixFormat"),
        _reserved_method("GetDeviceFormat"),
        _reserved_method("SetDeviceFormat"),
        _reserved_method("GetProcessingPeriod"),
        _reserved_method("SetProcessingPeriod"),
        _reserved_method("GetShareMode"),
        _reserved_method("SetShareMode"),
        _reserved_method("GetPropertyValue"),
        _reserved_method("SetPropertyValue"),
        COMMETHOD(
            [],
            HRESULT,
            "SetDefaultEndpoint",
            (["in"], c_wchar_p, "pszDeviceName"),
            (["in"], c_uint, "eRole"),
        ),
    ]


def com_task(func):
    """Инициализирует COM в фоновом потоке, чтобы pycaw и WMI работали за пределами главного потока."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        comtypes.CoInitialize()
        try:
            return func(*args, **kwargs)
        finally:
            comtypes.CoUninitialize()

    return wrapper


def probe_monitors():
    try:
        monitors = list(get_monitors())
    except Exception as exc:
        log.error("Не удалось получить список мониторов: %s", exc)
        return []

    entries = []
    for index, monitor in enumerate(monitors, start=1):
        entry = {"monitor": monitor, "brightness": None, "contrast": None, "error": None}
        try:
            with monitor:
                entry["brightness"] = monitor.get_luminance()
                entry["contrast"] = monitor.get_contrast()
        except Exception as exc:
            entry["error"] = str(exc)
            log.warning("Монитор %d не отвечает на команды DDC/CI: %s", index, exc)
        entries.append(entry)
    return entries


def write_monitor_value(monitor, feature, value):
    with monitor:
        getattr(monitor, MONITOR_SETTERS[feature])(value)


def read_pointer_speed():
    speed = c_int(0)
    if not ctypes.windll.user32.SystemParametersInfoW(SPI_GETMOUSESPEED, 0, ctypes.byref(speed), 0):
        raise ctypes.WinError()
    return speed.value


def write_pointer_speed(speed):
    flags = SPIF_UPDATEINIFILE | SPIF_SENDCHANGE
    if not ctypes.windll.user32.SystemParametersInfoW(SPI_SETMOUSESPEED, 0, c_void_p(speed), flags):
        raise ctypes.WinError()


@com_task
def read_volume():
    endpoint = AudioUtilities.GetSpeakers().EndpointVolume
    return round(endpoint.GetMasterVolumeLevelScalar() * 100), bool(endpoint.GetMute())


@com_task
def write_volume(percent):
    AudioUtilities.GetSpeakers().EndpointVolume.SetMasterVolumeLevelScalar(percent / 100, None)


@com_task
def write_mute(muted):
    AudioUtilities.GetSpeakers().EndpointVolume.SetMute(int(muted), None)


def press_media_key(key_code):
    user32 = ctypes.windll.user32
    user32.keybd_event(key_code, 0, 0, 0)
    user32.keybd_event(key_code, 0, KEYEVENTF_KEYUP, 0)


def _list_output_devices():
    devices = [(device.id, device.FriendlyName) for device in AudioUtilities.GetAllDevices()]
    default_id = AudioUtilities.GetDeviceEnumerator().GetDefaultAudioEndpoint(E_RENDER, E_MULTIMEDIA).GetId()
    return devices, default_id


@com_task
def read_default_output():
    devices, default_id = _list_output_devices()
    return next((name for device_id, name in devices if device_id == default_id), "не определено")


@com_task
def switch_output_device():
    devices, default_id = _list_output_devices()
    if not devices:
        raise RuntimeError("Устройства вывода не найдены")
    current = next((i for i, (device_id, _) in enumerate(devices) if device_id == default_id), -1)
    next_id, next_name = devices[(current + 1) % len(devices)]
    policy = CoCreateInstance(GUID(POLICY_CONFIG_CLSID), interface=IPolicyConfigVista, clsctx=CLSCTX_ALL)
    for role in DEVICE_ROLES:
        policy.SetDefaultEndpoint(next_id, role)
    return next_name


@com_task
def read_input_devices():
    connection = wmi.WMI()
    groups = (
        (connection.Win32_PointingDevice(), "Мышь"),
        (connection.Win32_Keyboard(), "Клавиатура"),
    )
    return [
        (item.Name or "Без имени", item.Status or "Неизвестно", kind)
        for items, kind in groups
        for item in items
    ]


@com_task
def read_usb_devices():
    rows = wmi.WMI().query("SELECT Name, Status, DeviceID FROM Win32_PnPEntity WHERE DeviceID LIKE 'USB%'")
    return sorted(((row.Name or "Без имени", row.Status or "Неизвестно") for row in rows), key=lambda r: r[0])


class PCControlApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("PC Control")
        self.geometry("980x660")
        self.minsize(860, 580)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.results = queue.Queue()
        self.ddc_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ddc")
        self.io_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="io")
        self.timers = {}
        self.monitor_entries = []

        ctk.CTkLabel(
            self,
            text="Панель управления ПК",
            font=ctk.CTkFont(size=24, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=24, pady=(20, 8))

        tabs = ctk.CTkTabview(self, corner_radius=12)
        tabs.pack(fill="both", expand=True, padx=24, pady=(0, 8))
        self._build_monitors_tab(tabs.add("Мониторы"))
        self._build_input_tab(tabs.add("Ввод"))
        self._build_pc_tab(tabs.add("ПК"))
        self._build_system_tab(tabs.add("Система"))

        self.status_label = ctk.CTkLabel(self, text="Готово", text_color=MUTED_COLOR, anchor="w")
        self.status_label.pack(fill="x", padx=24, pady=(0, 16))

        self.after(100, self._drain_results)
        self.scan_monitors()
        self.refresh_input()
        self.refresh_pointer()
        self.refresh_output()
        self.refresh_volume()
        self.refresh_usb()

    # Фоновые задачи и обмен результатами с интерфейсом

    def run_task(self, fn, *args, pool=None, on_done=None, on_error=None):
        future = (pool or self.io_pool).submit(fn, *args)
        future.add_done_callback(lambda f: self.results.put((f, on_done, on_error)))

    def _drain_results(self):
        while not self.results.empty():
            future, on_done, on_error = self.results.get()
            try:
                value = future.result()
            except Exception as exc:
                log.error("Операция завершилась с ошибкой: %s", exc, exc_info=exc)
                if on_error:
                    on_error(exc)
            else:
                if on_done:
                    on_done(value)
        self.after(100, self._drain_results)

    def debounce(self, key, action, delay=150):
        pending = self.timers.get(key)
        if pending:
            self.after_cancel(pending)
        self.timers[key] = self.after(delay, action)

    def set_status(self, text, warning=False):
        self.status_label.configure(text=text, text_color=WARNING_COLOR if warning else SUCCESS_COLOR)

    def _warn_on_error(self, message):
        return lambda _exc: self.set_status(message, warning=True)

    def _on_close(self):
        self.ddc_pool.shutdown(wait=False, cancel_futures=True)
        self.io_pool.shutdown(wait=False, cancel_futures=True)
        self.destroy()

    # Общие элементы интерфейса

    @staticmethod
    def _clear(frame):
        for widget in frame.winfo_children():
            widget.destroy()

    @staticmethod
    def _info_label(parent, text, color=MUTED_COLOR):
        ctk.CTkLabel(parent, text=text, text_color=color, anchor="w").pack(fill="x", padx=16, pady=16)

    @staticmethod
    def _section_header(parent, title, button_text, command):
        header = ctk.CTkFrame(parent, fg_color="transparent")
        header.pack(fill="x", padx=16, pady=(12, 4))
        ctk.CTkLabel(header, text=title, font=ctk.CTkFont(size=16, weight="bold")).pack(side="left")
        ctk.CTkButton(header, text=button_text, width=120, command=command).pack(side="right")

    @staticmethod
    def _card(parent, title):
        card = ctk.CTkFrame(parent, corner_radius=12)
        card.pack(fill="x", padx=8, pady=8)
        ctk.CTkLabel(card, text=title, font=ctk.CTkFont(size=15, weight="bold"), anchor="w").pack(
            fill="x", padx=16, pady=(14, 6)
        )
        return card

    @staticmethod
    def _slider_row(parent, title, minimum, maximum, initial, on_change, suffix=""):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=16, pady=(4, 10))
        ctk.CTkLabel(row, text=title, width=140, anchor="w").pack(side="left")
        value_label = ctk.CTkLabel(row, text=f"{initial}{suffix}", width=56)
        value_label.pack(side="right")

        def handle(raw):
            value = round(float(raw))
            value_label.configure(text=f"{value}{suffix}")
            on_change(value)

        slider = ctk.CTkSlider(row, from_=minimum, to=maximum, number_of_steps=maximum - minimum, command=handle)
        slider.set(initial)
        slider.pack(side="left", fill="x", expand=True, padx=12)
        return slider, value_label

    # Мониторы

    def _build_monitors_tab(self, tab):
        self._section_header(tab, "Яркость и контрастность (DDC/CI)", "Обновить", self.scan_monitors)
        self.monitor_list = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        self.monitor_list.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def scan_monitors(self):
        self._clear(self.monitor_list)
        self._info_label(self.monitor_list, "Поиск мониторов...")
        self.run_task(
            probe_monitors,
            on_done=self._render_monitors,
            on_error=self._warn_on_error("Не удалось выполнить поиск мониторов"),
        )

    def _render_monitors(self, entries):
        self._clear(self.monitor_list)
        self.monitor_entries = entries
        if not entries:
            self._info_label(self.monitor_list, "Мониторы с поддержкой DDC/CI не найдены")
            return
        for index, entry in enumerate(entries):
            card = ctk.CTkFrame(self.monitor_list, corner_radius=12)
            card.pack(fill="x", padx=8, pady=8)
            ctk.CTkLabel(card, text=f"Монитор {index + 1}", font=ctk.CTkFont(size=15, weight="bold"), anchor="w").pack(
                fill="x", padx=16, pady=(14, 6)
            )
            if entry["error"] is not None:
                ctk.CTkLabel(
                    card,
                    text="Не отвечает на команды DDC/CI. Регулировка недоступна.",
                    text_color=WARNING_COLOR,
                    anchor="w",
                ).pack(fill="x", padx=16, pady=(0, 14))
                continue
            self._slider_row(
                card, "Яркость", 0, 100, entry["brightness"],
                lambda v, i=index: self.queue_monitor_write(i, "brightness", v), suffix="%",
            )
            self._slider_row(
                card, "Контрастность", 0, 100, entry["contrast"],
                lambda v, i=index: self.queue_monitor_write(i, "contrast", v), suffix="%",
            )

    def queue_monitor_write(self, index, feature, value):
        monitor = self.monitor_entries[index]["monitor"]
        self.debounce(
            ("monitor", index, feature),
            lambda: self.run_task(
                write_monitor_value,
                monitor,
                feature,
                value,
                pool=self.ddc_pool,
                on_error=lambda _exc: self.set_status(
                    f"Монитор {index + 1} не принял команду DDC/CI", warning=True
                ),
            ),
        )

    # Ввод и мышь

    def _build_input_tab(self, tab):
        left = ctk.CTkFrame(tab, fg_color="transparent")
        left.pack(side="left", fill="both", expand=True, padx=(8, 4), pady=8)
        right = ctk.CTkFrame(tab, fg_color="transparent")
        right.pack(side="left", fill="both", expand=True, padx=(4, 8), pady=8)

        self._section_header(left, "Статус устройств ввода", "Обновить", self.refresh_input)
        self.input_list = ctk.CTkScrollableFrame(left, fg_color="transparent")
        self.input_list.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        mouse_card = self._card(right, "Чувствительность мыши")
        ctk.CTkLabel(
            mouse_card,
            text="Системная скорость указателя Windows (1-20).",
            text_color=MUTED_COLOR,
            anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 4))
        self.pointer_slider, self.pointer_value = self._slider_row(
            mouse_card, "Скорость", MIN_POINTER_SPEED, MAX_POINTER_SPEED, MIN_POINTER_SPEED, self._on_pointer_changed
        )

        actions_card = self._card(right, "Быстрые действия")
        ctk.CTkButton(
            actions_card, text="Очистить буфер обмена", height=36, command=self.clear_clipboard
        ).pack(fill="x", padx=16, pady=(0, 8))
        ctk.CTkButton(
            actions_card, text="Переключить аудио-выход", height=36, command=self.switch_output
        ).pack(fill="x", padx=16, pady=(0, 6))
        self.output_label = ctk.CTkLabel(actions_card, text="Сейчас: определяется...", text_color=MUTED_COLOR, anchor="w")
        self.output_label.pack(fill="x", padx=16, pady=(0, 14))

    def refresh_input(self):
        self.run_task(
            read_input_devices,
            on_done=self._render_input,
            on_error=self._warn_on_error("Не удалось получить статус устройств ввода"),
        )

    def _render_input(self, rows):
        self._clear(self.input_list)
        if not rows:
            self._info_label(self.input_list, "Устройства ввода не найдены")
            return
        for name, status, kind in rows:
            row = ctk.CTkFrame(self.input_list, corner_radius=8)
            row.pack(fill="x", padx=6, pady=4)
            ctk.CTkLabel(row, text=name, anchor="w").pack(side="left", fill="x", expand=True, padx=12, pady=10)
            ctk.CTkLabel(row, text=kind, text_color=MUTED_COLOR).pack(side="left", padx=8)
            color = SUCCESS_COLOR if status == "OK" else WARNING_COLOR
            ctk.CTkLabel(row, text=status, text_color=color).pack(side="right", padx=12)

    def refresh_pointer(self):
        self.run_task(
            read_pointer_speed,
            on_done=self._apply_pointer_speed,
            on_error=self._warn_on_error("Не удалось прочитать чувствительность мыши"),
        )

    def _apply_pointer_speed(self, speed):
        self.pointer_slider.set(speed)
        self.pointer_value.configure(text=str(speed))

    def _on_pointer_changed(self, speed):
        self.debounce(
            "pointer",
            lambda: self.run_task(
                write_pointer_speed,
                speed,
                on_done=lambda _: self.set_status(f"Скорость указателя: {speed}"),
                on_error=self._warn_on_error("Не удалось изменить чувствительность мыши"),
            ),
        )

    def clear_clipboard(self):
        self.clipboard_clear()
        self.set_status("Буфер обмена очищен")

    def refresh_output(self):
        self.run_task(
            read_default_output,
            on_done=self._show_output,
            on_error=self._warn_on_error("Не удалось определить аудио-выход"),
        )

    def _show_output(self, name):
        self.output_label.configure(text=f"Сейчас: {name}")

    def switch_output(self):
        self.run_task(
            switch_output_device,
            on_done=self._on_output_switched,
            on_error=self._warn_on_error("Не удалось переключить аудио-выход"),
        )

    def _on_output_switched(self, name):
        self._show_output(name)
        self.set_status(f"Аудио-выход переключен: {name}")

    # ПК: звук и медиа

    def _build_pc_tab(self, tab):
        volume_card = self._card(tab, "Системная громкость")
        self.volume_slider, self.volume_value = self._slider_row(
            volume_card, "Громкость", 0, 100, 0, self._on_volume_changed, suffix="%"
        )
        self.mute_switch = ctk.CTkSwitch(
            volume_card, text="Выключить звук", command=self._on_mute_toggled
        )
        self.mute_switch.pack(anchor="w", padx=16, pady=(0, 16))

        media_card = self._card(tab, "Медиаплеер")
        buttons = ctk.CTkFrame(media_card, fg_color="transparent")
        buttons.pack(fill="x", padx=16, pady=(0, 16))
        for text, key in (
            ("Назад", VK_MEDIA_PREV_TRACK),
            ("Пауза / Play", VK_MEDIA_PLAY_PAUSE),
            ("Вперед", VK_MEDIA_NEXT_TRACK),
        ):
            ctk.CTkButton(
                buttons,
                text=text,
                height=40,
                command=lambda k=key: self.run_task(
                    press_media_key,
                    k,
                    on_error=self._warn_on_error("Не удалось отправить команду медиаплееру"),
                ),
            ).pack(side="left", fill="x", expand=True, padx=4)

    def refresh_volume(self):
        self.run_task(
            read_volume,
            on_done=self._apply_volume,
            on_error=self._warn_on_error("Не удалось прочитать громкость"),
        )

    def _apply_volume(self, result):
        percent, muted = result
        self.volume_slider.set(percent)
        self.volume_value.configure(text=f"{percent}%")
        if muted:
            self.mute_switch.select()
        else:
            self.mute_switch.deselect()

    def _on_volume_changed(self, percent):
        self.debounce(
            "volume",
            lambda: self.run_task(
                write_volume,
                percent,
                on_error=self._warn_on_error("Не удалось изменить громкость"),
            ),
        )

    def _on_mute_toggled(self):
        muted = bool(self.mute_switch.get())
        self.run_task(
            write_mute,
            muted,
            on_error=self._warn_on_error("Не удалось переключить звук"),
        )

    # Система: USB

    def _build_system_tab(self, tab):
        self._section_header(tab, "Подключенные USB-устройства", "Обновить", self.refresh_usb)
        self.usb_list = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        self.usb_list.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def refresh_usb(self):
        self._clear(self.usb_list)
        self._info_label(self.usb_list, "Загрузка списка...")
        self.run_task(
            read_usb_devices,
            on_done=self._render_usb,
            on_error=self._warn_on_error("Не удалось получить список USB-устройств"),
        )

    def _render_usb(self, rows):
        self._clear(self.usb_list)
        if not rows:
            self._info_label(self.usb_list, "USB-устройства не найдены")
            return
        for name, status in rows:
            row = ctk.CTkFrame(self.usb_list, corner_radius=8)
            row.pack(fill="x", padx=6, pady=4)
            ctk.CTkLabel(row, text=name, anchor="w").pack(side="left", fill="x", expand=True, padx=12, pady=10)
            color = SUCCESS_COLOR if status == "OK" else WARNING_COLOR
            ctk.CTkLabel(row, text=status, text_color=color).pack(side="right", padx=12)


if __name__ == "__main__":
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    PCControlApp().mainloop()
