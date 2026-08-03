# Intentionally empty: individual router modules are imported directly by whoever needs
# them (app.py). Making this file eager-import every submodule pulls SQLAlchemy in even
# for demo-mode deployments that don't have it installed.
