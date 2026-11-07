"""rk-deploy: deploy RKNN apps to Rockchip boards as systemd services.

Controller side (this package) packs an application described by a
manifest, pushes it to a small fleet of boards over SSH, and inspects
the fleet. The board side is a set of POSIX shell scripts shipped in
``rkdeploy.board`` that run on the board with no extra dependencies.
"""

__version__ = "0.2.0"
