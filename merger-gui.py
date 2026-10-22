"""SmoothUIMerger.exe (windowed): the merger's window. See smoothui/merger/gui.py.

    SmoothUIMerger.exe            (the window: scan, merge)
    SmoothUIMerger.exe --stale    (opened by SmoothUILauncher.exe before the game starts)

Everything happens under the __main__ check: the process pool's workers start this script again (as
__mp_main__), and must not import the window.
"""

if __name__ == "__main__":
    import multiprocessing
    import sys

    multiprocessing.freeze_support()    # first: in a frozen worker, this runs the worker instead

    from smoothui.merger.gui import LOG_FILE, main
    from smoothui.windows import MB_ICONERROR, msg_box

    try:
        code = main()
    except Exception as e:  # a windowed exe has no console
        import logging
        logging.getLogger("smoothui.merger.gui").exception("Unexpected error")
        msg_box(f"Smooth UI's merger failed:\n\n{type(e).__name__}: {e}\n\nDetails are in data\\{LOG_FILE}.",
                MB_ICONERROR)
        code = 1
    sys.exit(code)
