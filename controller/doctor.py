"""``wakeci doctor`` on the controller host, where the package is not on the path:

    sudo -u wakeci python3 /opt/wakeci/controller/doctor.py /etc/wakeci/config.json [--probe]

It finds the package next to this directory, like check_config.py. See wakeci/doctor.py.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from wakeci import cli  # noqa: E402

if __name__ == "__main__":
    sys.exit(cli.main(["doctor", *sys.argv[1:]]))
