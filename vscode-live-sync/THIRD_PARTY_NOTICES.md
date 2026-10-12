# Third-party notices

The bridge source is covered by this project's MIT license.

`build_windows_launcher.py` obtains the native Windows launcher from the user's installed `pip._vendor.distlib` package. No upstream executable is included in the source release. Locally generated executables and VSIX packages contain that distlib launcher; the builder includes the upstream [distlib license](DISTLIB-LICENSE.txt) and these notices in the VSIX.

Distlib is maintained by Vinay Sajip and the Python Packaging Authority: <https://github.com/pypa/distlib>. Its applicable copyright and license text are reproduced in `DISTLIB-LICENSE.txt`.

The native Windows launcher additionally carries Vinay Sajip's BSD 2-Clause notice from [PC/launcher.c](https://github.com/pypa/distlib/blob/master/PC/launcher.c), reproduced in `WINDOWS-LAUNCHER-LICENSE.txt`. The builder includes that notice in locally generated VSIX packages.

The official OpenAI Codex extension and CLI are found in the user's existing installation; they are neither modified nor redistributed by this package.
