from colorama import just_fix_windows_console

from smoothui.build.main import main

if __name__ == "__main__":
    just_fix_windows_console() # Initialize colorama
    main()