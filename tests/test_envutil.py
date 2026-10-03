from quotaglance.envutil import host_environment, overridden_names
from quotaglance.integration import desktop_entry


def test_host_environment_restores_and_unsets():
    env = {
        "LD_LIBRARY_PATH": "/appdir/usr/lib",
        "PYTHONHOME": "/appdir/usr",
        "QUOTAGLANCE_HOST_PYTHONHOME": "__QG_UNSET__",
        "XDG_DATA_DIRS": "/appdir/usr/share:/usr/share",
        "QUOTAGLANCE_HOST_XDG_DATA_DIRS": "/usr/share",
        "HOME": "/home/me",
    }
    clean = host_environment(env)
    assert "PYTHONHOME" not in clean
    assert clean["XDG_DATA_DIRS"] == "/usr/share"
    assert clean["HOME"] == "/home/me"
    assert not any(k.startswith("QUOTAGLANCE_HOST_") for k in clean)
    assert overridden_names(env) == {"PYTHONHOME": None, "XDG_DATA_DIRS": "/usr/share"}


def test_appimage_desktop_entry_points_at_the_image():
    template = ("[Desktop Entry]\nName=QuotaGlance\nExec=quotaglance\nDBusActivatable=true\n\n"
                "[Desktop Action widget]\nExec=quotaglance --widget\n")
    entry = desktop_entry("/home/me/Apps/QuotaGlance 0.1.AppImage", template)
    assert "Exec='/home/me/Apps/QuotaGlance 0.1.AppImage'\n" in entry
    assert "Exec='/home/me/Apps/QuotaGlance 0.1.AppImage' --widget" in entry
    assert "DBusActivatable=false" in entry
