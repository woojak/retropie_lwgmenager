#!/usr/bin/env python3
import os
import posixpath
import shutil
import psutil
import subprocess
import tempfile
from datetime import datetime
from flask import Flask, request, render_template_string, redirect, url_for, send_from_directory, flash, abort, Response, jsonify
from functools import wraps
from werkzeug.utils import secure_filename

# --- Configuration file handling ---
CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.cfg")

def load_config():
    # Default configuration values
    config = {
        "login": "admin",
        "password": "mawerik1",
        "secret_key": "your_secret_key",
        "port": 5000,
        "monitor_refresh": 0.5
    }
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip()
                    if key in ["port"]:
                        try:
                            config[key] = int(val)
                        except ValueError:
                            pass
                    elif key in ["monitor_refresh"]:
                        try:
                            config[key] = float(val)
                        except ValueError:
                            pass
                    else:
                        config[key] = val
    else:
        save_config(config)
    return config

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        for key, val in config.items():
            f.write(f"{key}={val}\n")

CONFIG = load_config()

# --- Set up a custom temporary directory ---
TEMP_DIR = '/home/pi/tmp'
if not os.path.exists(TEMP_DIR):
    os.makedirs(TEMP_DIR, mode=0o1777)
os.environ['TMPDIR'] = TEMP_DIR
tempfile.tempdir = TEMP_DIR

app = Flask(__name__)
app.secret_key = CONFIG["secret_key"]

# Global authentication values from CONFIG
USERNAME = CONFIG["login"]
PASSWORD = CONFIG["password"]

# Base directory for file management
BASE_DIR = os.path.abspath("/home/pi/RetroPie")

# --- Jinja2 Filters ---
def format_datetime(value):
    return datetime.fromtimestamp(value).strftime('%Y-%m-%d %H:%M:%S')

def format_filesize(value):
    if value is None:
        return ''
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if value < 1024:
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} PB"

app.jinja_env.filters['datetimeformat'] = format_datetime
app.jinja_env.filters['filesizeformat'] = format_filesize

# --- Authentication Functions ---
def check_auth(username, password):
    return username == CONFIG["login"] and password == CONFIG["password"]

def authenticate():
    return Response(
        'Access denied. Please log in with the correct credentials.\n', 401,
        {'WWW-Authenticate': 'Basic realm="Login Required"'}
    )

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

def safe_path(path):
    abs_path = os.path.abspath(os.path.join(BASE_DIR, path))
    if os.path.commonpath([abs_path, BASE_DIR]) != BASE_DIR:
        abort(403)
    return abs_path

# --- Helper Functions ---
def round_1(x):
    return round(x, 1)

def get_cpu_temp():
    try:
        temp_str = subprocess.check_output(['vcgencmd', 'measure_temp']).decode().strip()
        return temp_str.split('=')[1].split("'")[0] + "°C"
    except Exception:
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                temp_val = f.read().strip()
                return str(round(float(temp_val) / 1000, 1)) + "°C"
        except Exception:
            return "N/A"

def get_ssd_temperatures():
    sensors = {}
    try:
        output = subprocess.check_output(
            ["sudo", "nvme", "smart-log", "/dev/nvme0"],
            stderr=subprocess.STDOUT,
            universal_newlines=True
        )
        for line in output.splitlines():
            if "temperature" in line.lower():
                parts = line.split(":")
                if len(parts) >= 2:
                    sensor_name = parts[0].strip()
                    temp_str = parts[1].strip()  # e.g., "28 C (301 K)"
                    tokens = temp_str.split()
                    if tokens:
                        raw_val = tokens[0].lower().replace("°c", "").replace("c", "").strip()
                        if raw_val.replace('.', '', 1).isdigit():
                            sensors[sensor_name] = raw_val + "°C"
                        else:
                            sensors[sensor_name] = tokens[0].replace("C", "").replace("c", "") + "°C"
        return sensors
    except Exception:
        return {}

def get_monitoring_data(selected_sensor=None):
    cpu_usage = round_1(psutil.cpu_percent(interval=0.0))
    mem = psutil.virtual_memory()
    mem_percent = round_1(mem.percent)
    disk = psutil.disk_usage(BASE_DIR)
    disk_percent = round_1((disk.used / disk.total) * 100) if disk.total > 0 else 0
    disk_free = disk.total - disk.used
    cpu_temp = get_cpu_temp()
    ssd_temps = get_ssd_temperatures()
    if not ssd_temps:
        ssd_selected_name = None
        ssd_selected_temp = "N/A"
    else:
        if selected_sensor and selected_sensor in ssd_temps:
            ssd_selected_name = selected_sensor
            ssd_selected_temp = ssd_temps[selected_sensor]
        else:
            ssd_selected_name = list(ssd_temps.keys())[0]
            ssd_selected_temp = ssd_temps[ssd_selected_name]
    return {
        'cpu_usage': cpu_usage,
        'cpu_temp': cpu_temp,
        'mem_total': mem.total,
        'mem_used': mem.used,
        'mem_percent': mem_percent,
        'disk_total': disk.total,
        'disk_used': disk.used,
        'disk_free': disk_free,
        'disk_percent': disk_percent,
        'ssd_all': ssd_temps,
        'ssd_selected_name': ssd_selected_name,
        'ssd_temp': ssd_selected_temp
    }

# --- Monitoring API Endpoint ---
@app.route('/api/monitoring')
@requires_auth
def api_monitoring():
    selected_sensor = request.args.get('ssd_sensor', None)
    mon = get_monitoring_data(selected_sensor)
    response = {
        'cpu_usage': mon['cpu_usage'],
        'cpu_temp': mon['cpu_temp'],
        'mem_percent': mon['mem_percent'],
        'disk_percent': mon['disk_percent'],
        'disk_used_human': format_filesize(mon['disk_used']),
        'disk_total_human': format_filesize(mon['disk_total']),
        'disk_free_human': format_filesize(mon['disk_free']),
        'ssd_temp': mon['ssd_temp'],
        'ssd_selected_name': mon['ssd_selected_name'],
        'ssd_all': mon['ssd_all']
    }
    return jsonify(response)

# --- Control Endpoint ---
@app.route('/control', methods=['POST'])
@requires_auth
def control():
    action = request.form.get("action")
    if action == "reboot":
        flash("Rebooting Raspberry Pi...")
        subprocess.call(["reboot"])
    elif action == "shutdown":
        flash("Shutting down Raspberry Pi...")
        subprocess.call(["shutdown", "now"])
    else:
        flash("Invalid action.")
    return redirect(url_for('dir_listing', req_path=""))

# --- Settings Endpoint ---
@app.route('/settings', methods=['GET', 'POST'])
@requires_auth
def settings():
    global CONFIG, USERNAME, PASSWORD, app
    message = ""
    if request.method == 'POST':
        if 'save_credentials' in request.form:
            new_login = request.form.get('login', '').strip()
            new_password = request.form.get('password', '').strip()
            if new_login:
                CONFIG['login'] = new_login
                USERNAME = new_login
            if new_password:
                CONFIG['password'] = new_password
                PASSWORD = new_password
            message += "Credentials updated. "
        elif 'save_app_settings' in request.form:
            new_secret_key = request.form.get('secret_key', '').strip()
            new_port = request.form.get('port', '').strip()
            new_refresh = request.form.get('monitor_refresh', '').strip()
            if new_secret_key:
                CONFIG['secret_key'] = new_secret_key
                app.secret_key = new_secret_key
            if new_port.isdigit():
                CONFIG['port'] = int(new_port)
            try:
                new_refresh_val = float(new_refresh)
                if new_refresh_val >= 0.5:
                    CONFIG['monitor_refresh'] = new_refresh_val
            except Exception:
                pass
            message += "App settings updated. (Port changes will take effect on restart.) "
        elif 'restart_service' in request.form:
            subprocess.call(["systemctl", "restart", "web_panel.service"])
            message += "Service restarted. "
        elif 'enable_service' in request.form:
            subprocess.call(["systemctl", "enable", "web_panel.service"])
            message += "Service enabled. "
        elif 'disable_service' in request.form:
            subprocess.call(["systemctl", "disable", "web_panel.service"])
            message += "Service disabled. "
        save_config(CONFIG)
        flash(message)
        return redirect(url_for('settings'))
    settings_template = """
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <title>Settings</title>
      <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.1/dist/css/bootstrap.min.css" rel="stylesheet">
      <style>
        .nav-tabs .nav-link { cursor: pointer; }
      </style>
    </head>
    <body>
      <div class="container py-4">
        <h1>Settings</h1>
        <ul class="nav nav-tabs" id="settingsTab" role="tablist">
          <li class="nav-item" role="presentation">
            <button class="nav-link active" id="credentials-tab" data-bs-toggle="tab" data-bs-target="#credentials" type="button" role="tab" aria-controls="credentials" aria-selected="true">Credentials</button>
          </li>
          <li class="nav-item" role="presentation">
            <button class="nav-link" id="app-settings-tab" data-bs-toggle="tab" data-bs-target="#app-settings" type="button" role="tab" aria-controls="app-settings" aria-selected="false">App Settings</button>
          </li>
          <li class="nav-item" role="presentation">
            <button class="nav-link" id="service-tab" data-bs-toggle="tab" data-bs-target="#service" type="button" role="tab" aria-controls="service" aria-selected="false">Service Management</button>
          </li>
        </ul>
        <div class="tab-content" id="settingsTabContent">
          <div class="tab-pane fade show active pt-3" id="credentials" role="tabpanel" aria-labelledby="credentials-tab">
            <form method="post">
              <div class="mb-3">
                <label for="login" class="form-label">Login</label>
                <input type="text" class="form-control" id="login" name="login" value="{{ config['login'] }}">
              </div>
              <div class="mb-3">
                <label for="password" class="form-label">Password</label>
                <input type="text" class="form-control" id="password" name="password" value="{{ config['password'] }}">
              </div>
              <button type="submit" name="save_credentials" class="btn btn-primary">Save Credentials</button>
            </form>
          </div>
          <div class="tab-pane fade pt-3" id="app-settings" role="tabpanel" aria-labelledby="app-settings-tab">
            <form method="post">
              <div class="mb-3">
                <label for="secret_key" class="form-label">Secret Key</label>
                <input type="text" class="form-control" id="secret_key" name="secret_key" value="{{ config['secret_key'] }}">
              </div>
              <div class="mb-3">
                <label for="port" class="form-label">Port</label>
                <input type="number" class="form-control" id="port" name="port" value="{{ config['port'] }}">
              </div>
              <div class="mb-3">
                <label for="monitor_refresh" class="form-label">Monitoring Refresh Interval (seconds, min 0.5)</label>
                <input type="number" step="0.1" class="form-control" id="monitor_refresh" name="monitor_refresh" value="{{ config['monitor_refresh'] }}">
              </div>
              <button type="submit" name="save_app_settings" class="btn btn-primary">Save App Settings</button>
            </form>
          </div>
          <div class="tab-pane fade pt-3" id="service" role="tabpanel" aria-labelledby="service-tab">
            <form method="post">
              <div class="mb-3">
                <button type="submit" name="restart_service" class="btn btn-warning mb-2">Restart Service</button>
              </div>
              <div class="mb-3">
                <button type="submit" name="enable_service" class="btn btn-success mb-2">Enable Service</button>
              </div>
              <div class="mb-3">
                <button type="submit" name="disable_service" class="btn btn-danger mb-2">Disable Service</button>
              </div>
            </form>
          </div>
        </div>
        <br>
        <a href="{{ url_for('dir_listing', req_path='') }}" class="btn btn-secondary">Back to File Manager</a>
      </div>
      <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.1/dist/js/bootstrap.bundle.min.js"></script>
    </body>
    </html>
    """
    return render_template_string(settings_template, config=CONFIG)

# ------------------ File Editing Endpoint (within BASE_DIR) ------------------ #
@app.route('/edit/<path:req_path>', methods=['GET', 'POST'])
@requires_auth
def edit_file(req_path):
    abs_path = safe_path(req_path)
    if not os.path.isfile(abs_path):
        flash("The selected item is not a file.")
        return redirect(url_for('dir_listing', req_path=posixpath.dirname(req_path)))
    if request.method == 'POST':
        new_content = request.form.get('content', '')
        try:
            with open(abs_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            flash("File has been updated.")
            return redirect(url_for('dir_listing', req_path=posixpath.dirname(req_path)))
        except Exception as e:
            flash(f"Error saving the file: {e}")
    else:
        try:
            with open(abs_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            flash(f"Error reading the file: {e}")
            return redirect(url_for('dir_listing', req_path=posixpath.dirname(req_path)))
        edit_template = """
        <!doctype html>
        <html lang="en">
        <head>
          <meta charset="utf-8">
          <title>Edit File</title>
          <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.1/dist/css/bootstrap.min.css" rel="stylesheet">
        </head>
        <body>
          <div class="container py-4">
            <h1>Edit file: {{ filename }}</h1>
            <form method="post">
              <div class="mb-3">
                <textarea name="content" class="form-control" rows="20">{{ content }}</textarea>
              </div>
              <button type="submit" class="btn btn-primary">Save Changes</button>
              <a href="{{ url_for('dir_listing', req_path=parent_path) }}" class="btn btn-secondary">Cancel</a>
            </form>
          </div>
          <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.1/dist/js/bootstrap.bundle.min.js"></script>
        </body>
        </html>
        """
        return render_template_string(edit_template, filename=os.path.basename(abs_path),
                                      content=content, parent_path=posixpath.dirname(req_path))

# ------------------ Endpoint for Editing /boot/firmware/config.txt ------------------ #
@app.route('/edit_config', methods=['GET', 'POST'])
@requires_auth
def edit_config():
    config_path = '/boot/firmware/config.txt'
    if request.method == 'POST':
        new_content = request.form.get('content', '')
        try:
            with open(config_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            flash("config.txt has been updated.")
            return redirect(url_for('dir_listing', req_path=''))
        except Exception as e:
            flash(f"Error saving config.txt: {e}")
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        flash(f"Error reading config.txt: {e}")
        return redirect(url_for('dir_listing', req_path=''))
    edit_template = """
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <title>Edit RPI config</title>
      <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.1/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body>
      <div class="container py-4">
        <h1>Edit file: config.txt</h1>
        <form method="post">
          <div class="mb-3">
            <textarea name="content" class="form-control" rows="20">{{ content }}</textarea>
          </div>
          <button type="submit" class="btn btn-primary">Save Changes</button>
          <a href="{{ url_for('dir_listing', req_path='') }}" class="btn btn-secondary">Cancel</a>
        </form>
      </div>
      <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.1/dist/js/bootstrap.bundle.min.js"></script>
    </body>
    </html>
    """
    return render_template_string(edit_template, content=content)

# ------------------ Bulk Delete Endpoint ------------------ #
@app.route('/delete_bulk', methods=['POST'])
@requires_auth
def delete_bulk():
    selected = request.form.getlist('selected_files')
    for file_rel in selected:
        abs_path = safe_path(file_rel)
        try:
            if os.path.isdir(abs_path):
                shutil.rmtree(abs_path)
            elif os.path.isfile(abs_path):
                os.remove(abs_path)
        except Exception as e:
            flash(f"Error deleting {file_rel}: {e}")
    flash("Selected items have been deleted.")
    parent = posixpath.dirname(selected[0]) if selected else ''
    return redirect(url_for('dir_listing', req_path=parent))

# ------------------ Folder Creation Endpoint ------------------ #
@app.route('/create_folder/<path:req_path>', methods=['POST'])
@requires_auth
def create_folder(req_path):
    abs_path = safe_path(req_path)
    folder_name = request.form.get('folder_name', '').strip()
    if folder_name == '':
        flash("Folder name is empty.")
        return redirect(url_for('dir_listing', req_path=req_path))
    new_dir = os.path.join(abs_path, secure_filename(folder_name))
    try:
        os.makedirs(new_dir)
        flash("Folder created successfully.")
    except Exception as e:
        flash(f"Error creating folder: {e}")
    return redirect(url_for('dir_listing', req_path=req_path))

# ------------------ File Upload Endpoint (Manual Saving in Chunks) ------------------ #
@app.route('/upload/<path:req_path>', methods=['POST'])
@requires_auth
def upload_file(req_path):
    abs_dir = safe_path(req_path)
    if 'file' not in request.files:
        flash("No file selected.")
        return redirect(url_for('dir_listing', req_path=req_path))
    uploaded_files = request.files.getlist('file')
    for file in uploaded_files:
        if file.filename == '':
            flash("One of the uploaded files has no name.")
            continue
        filename = secure_filename(file.filename)
        save_path = os.path.join(abs_dir, filename)
        try:
            with open(save_path, 'wb') as f:
                while True:
                    chunk = file.stream.read(8192)
                    if not chunk:
                        break
                    f.write(chunk)
            flash(f"File '{filename}' uploaded successfully.")
        except Exception as e:
            flash(f"Error saving file '{filename}': {e}")
    return redirect(url_for('dir_listing', req_path=req_path))

# ------------------ File Deletion Endpoint ------------------ #
@app.route('/delete/<path:req_path>')
@requires_auth
def delete_file(req_path):
    abs_path = safe_path(req_path)
    if not os.path.isfile(abs_path):
        flash("The selected item is not a file or does not exist.")
    else:
        try:
            os.remove(abs_path)
            flash("File has been deleted.")
        except Exception as e:
            flash(f"Error deleting file: {e}")
    parent = posixpath.dirname(req_path)
    return redirect(url_for('dir_listing', req_path=parent))

# ------------------ Folder Deletion Endpoint ------------------ #
@app.route('/delete_folder/<path:req_path>')
@requires_auth
def delete_folder(req_path):
    abs_path = safe_path(req_path)
    if not os.path.isdir(abs_path):
        flash("The selected item is not a folder or does not exist.")
    else:
        try:
            shutil.rmtree(abs_path)
            flash("Folder has been deleted.")
        except Exception as e:
            flash(f"Error deleting folder: {e}")
    parent = posixpath.dirname(req_path)
    return redirect(url_for('dir_listing', req_path=parent))

# ------------------ Main Directory Listing Endpoint ------------------ #
@app.route('/', defaults={'req_path': ''})
@app.route('/<path:req_path>')
@requires_auth
def dir_listing(req_path):
    abs_path = safe_path(req_path)
    if not os.path.exists(abs_path):
        return f"Directory or file does not exist: {req_path}", 404
    if os.path.isfile(abs_path):
        return send_from_directory(os.path.dirname(abs_path), os.path.basename(abs_path), as_attachment=True)

    # Process the selected SSD sensor.
    selected_sensor = request.args.get('ssd_sensor', None)
    sensor_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "selected_sensor.txt")
    if selected_sensor:
        with open(sensor_file, "w") as sf:
            sf.write(selected_sensor)
    else:
        if os.path.exists(sensor_file):
            selected_sensor = open(sensor_file, "r").read().strip()

    monitoring_info = get_monitoring_data(selected_sensor)
    files = []
    try:
        for filename in os.listdir(abs_path):
            path = os.path.join(req_path, filename)
            full_path = os.path.join(abs_path, filename)
            file_info = {
                'name': filename,
                'path': path,
                'is_dir': os.path.isdir(full_path),
                'mtime': os.path.getmtime(full_path),
                'size': os.path.getsize(full_path) if os.path.isfile(full_path) else None,
                'file_type': 'folder' if os.path.isdir(full_path) else os.path.splitext(filename)[1].lower()
            }
            files.append(file_info)
    except PermissionError:
        flash("Insufficient permissions to read directory contents.")
        files = []
    sort_by = request.args.get('sort', 'name')
    order = request.args.get('order', 'asc')
    reverse_order = (order == 'desc')
    if sort_by == 'date':
        files.sort(key=lambda x: x['mtime'], reverse=reverse_order)
    elif sort_by == 'type':
        files.sort(key=lambda x: (x['file_type'], x['name'].lower()), reverse=reverse_order)
    elif sort_by == 'size':
        files.sort(key=lambda x: x['size'] if x['size'] is not None else 0, reverse=reverse_order)
    else:
        files.sort(key=lambda x: x['name'].lower(), reverse=reverse_order)
    parent_path = posixpath.dirname(req_path)
    html_template = """
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <title>RetroPie Light Web Game Manager</title>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.1/dist/css/bootstrap.min.css" rel="stylesheet">
      <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" rel="stylesheet">
      <style>
         canvas { max-width: 300px; max-height: 300px; }
         .progress { height: 40px; }
         .progress-bar { color: black; font-size: 1.2em; font-weight: bold; }
      </style>
    </head>
    <body>
      <div class="container py-4">
        <h1 class="mb-4">RetroPie Light Web Game Manager</h1>
        <div class="text-end mb-3">
          <a href="{{ url_for('edit_config') }}" class="btn btn-outline-warning">
            <i class="fas fa-edit"></i> Edit config.txt
          </a>
          <a href="{{ url_for('settings') }}" class="btn btn-outline-secondary">
            <i class="fas fa-cog"></i> Settings
          </a>
        </div>
        {% with messages = get_flashed_messages() %}
          {% if messages %}
            {% for message in messages %}
              <div class="alert alert-warning" role="alert">{{ message }}</div>
            {% endfor %}
          {% endif %}
        {% endwith %}
        
        <!-- Monitoring Section with Progress Bars -->
        <div class="card mb-4">
          <div class="card-header">
            <h3>Monitoring</h3>
          </div>
          <div class="card-body">
            <div class="row mb-3">
              <div class="col-md-3">
                <strong>CPU Temp:</strong>
                <span id="cpu-temp">{{ monitoring_info.cpu_temp }}</span>
              </div>
              <div class="col-md-3">
                <strong>SSD Temp:</strong>
                <span id="ssd-temp">{{ monitoring_info.ssd_temp }}</span>
                {% if monitoring_info.ssd_selected_name %}
                  <br><small>{{ monitoring_info.ssd_selected_name }}</small>
                {% endif %}
              </div>
              <div class="col-md-6">
                {% if monitoring_info.ssd_all %}
                  <form method="get" class="d-flex align-items-center">
                    <label for="ssd_sensor_select" class="me-2"><small>Select SSD Sensor:</small></label>
                    <input type="hidden" name="req_path" value="{{ req_path }}">
                    <select name="ssd_sensor" id="ssd_sensor_select" class="form-select" style="width:auto;" onchange="this.form.submit()">
                      {% for sensor, temp in monitoring_info.ssd_all.items() %}
                        <option value="{{ sensor }}" {% if sensor == monitoring_info.ssd_selected_name %}selected{% endif %}>{{ sensor }}: {{ temp }}</option>
                      {% endfor %}
                    </select>
                  </form>
                {% endif %}
              </div>
            </div>
            <div class="row mb-3">
              <div class="col-md-4">
                <div class="mb-1"><strong>CPU Usage:</strong></div>
                <div class="progress">
                  <div id="cpuBar" class="progress-bar" role="progressbar" style="width: {{ monitoring_info.cpu_usage }}%;" aria-valuenow="{{ monitoring_info.cpu_usage }}" aria-valuemin="0" aria-valuemax="100">{{ monitoring_info.cpu_usage }}%</div>
                </div>
              </div>
              <div class="col-md-4">
                <div class="mb-1"><strong>Memory Usage:</strong></div>
                <div class="progress">
                  <div id="memBar" class="progress-bar bg-success" role="progressbar" style="width: {{ monitoring_info.mem_percent }}%;" aria-valuenow="{{ monitoring_info.mem_percent }}" aria-valuemin="0" aria-valuemax="100">{{ monitoring_info.mem_percent }}%</div>
                </div>
              </div>
              <div class="col-md-4">
                <div class="mb-1"><strong>Disk Usage:</strong> ({{ monitoring_info.disk_used | filesizeformat }} used / {{ monitoring_info.disk_total | filesizeformat }} total)</div>
                <div class="progress">
                  <div id="diskBar" class="progress-bar bg-info" role="progressbar" style="width: {{ monitoring_info.disk_percent }}%;" aria-valuenow="{{ monitoring_info.disk_percent }}" aria-valuemin="0" aria-valuemax="100">{{ monitoring_info.disk_percent }}%</div>
                </div>
              </div>
            </div>
          </div>
        </div>
        
        <!-- Control Section -->
        <div class="card mb-4">
          <div class="card-header">
            <h3>Control</h3>
          </div>
          <div class="card-body">
            <form method="post" action="{{ url_for('control') }}">
              <button type="submit" name="action" value="reboot" class="btn btn-warning mb-2">Reboot Raspberry Pi</button>
              <button type="submit" name="action" value="shutdown" class="btn btn-danger mb-2">Shutdown Raspberry Pi</button>
            </form>
          </div>
        </div>
        
        <!-- Upload and Create Folder Section -->
        <div class="card mb-4">
          <div class="card-header">
            <h3>Upload Files / Create Folder</h3>
          </div>
          <div class="card-body">
            <div class="row">
              <div class="col-md-6">
                <form id="uploadForm" action="{{ url_for('upload_file', req_path=req_path) }}" method="post" enctype="multipart/form-data">
                  <div class="mb-3">
                    <input type="file" name="file" class="form-control" multiple>
                  </div>
                  <button type="submit" class="btn btn-primary"><i class="fas fa-upload"></i> Upload</button>
                </form>
                <div id="uploadProgress" class="progress mt-2" style="display:none;">
                  <div id="uploadProgressBar" class="progress-bar" role="progressbar" style="width: 0%;">0%</div>
                </div>
              </div>
              <div class="col-md-6">
                <form action="{{ url_for('create_folder', req_path=req_path) }}" method="post">
                  <div class="mb-3">
                    <input type="text" name="folder_name" placeholder="Folder Name" class="form-control">
                  </div>
                  <button type="submit" class="btn btn-secondary"><i class="fas fa-folder-plus"></i> Create Folder</button>
                </form>
              </div>
            </div>
          </div>
        </div>
        
        <!-- Bulk Selection and File/Folder List Section -->
        <form method="post" action="{{ url_for('delete_bulk') }}">
          <div class="card">
            <div class="card-header">
              <h3>File and Folder List</h3>
              <div class="mt-2">
                <span>Sort by: </span>
                <a href="{{ url_for('dir_listing', req_path=req_path, sort='name', order='asc') }}" class="btn btn-sm btn-outline-primary">Name ↑</a>
                <a href="{{ url_for('dir_listing', req_path=req_path, sort='name', order='desc') }}" class="btn btn-sm btn-outline-primary">Name ↓</a>
                <a href="{{ url_for('dir_listing', req_path=req_path, sort='date', order='asc') }}" class="btn btn-sm btn-outline-secondary">Date ↑</a>
                <a href="{{ url_for('dir_listing', req_path=req_path, sort='date', order='desc') }}" class="btn btn-sm btn-outline-secondary">Date ↓</a>
                <a href="{{ url_for('dir_listing', req_path=req_path, sort='type', order='asc') }}" class="btn btn-sm btn-outline-info">Type ↑</a>
                <a href="{{ url_for('dir_listing', req_path=req_path, sort='type', order='desc') }}" class="btn btn-sm btn-outline-info">Type ↓</a>
                <a href="{{ url_for('dir_listing', req_path=req_path, sort='size', order='asc') }}" class="btn btn-sm btn-outline-dark">Size ↑</a>
                <a href="{{ url_for('dir_listing', req_path=req_path, sort='size', order='desc') }}" class="btn btn-sm btn-outline-dark">Size ↓</a>
              </div>
            </div>
            <div class="card-body">
              <nav aria-label="breadcrumb">
                <ol class="breadcrumb">
                  <li class="breadcrumb-item">
                    <a href="{{ url_for('dir_listing', req_path='') }}"><i class="fas fa-home"></i> Home</a>
                  </li>
                  {% if req_path %}
                    {% set parts = req_path.split('/') %}
                    {% set path_acc = "" %}
                    {% for part in parts %}
                      {% set path_acc = path_acc + part %}
                      <li class="breadcrumb-item">
                        <a href="{{ url_for('dir_listing', req_path=path_acc) }}">{{ part }}</a>
                      </li>
                      {% set path_acc = path_acc + "/" %}
                    {% endfor %}
                  {% endif %}
                </ol>
              </nav>
              <table class="table table-striped table-hover">
                <thead>
                  <tr>
                    <th>Select</th>
                    <th>Icon</th>
                    <th>Name</th>
                    <th>Type</th>
                    <th>Modified Date</th>
                    <th>Size</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {% if req_path %}
                  <tr>
                    <td colspan="7">
                      <a href="{{ url_for('dir_listing', req_path=parent_path) }}" class="btn btn-sm btn-outline-dark">
                        <i class="fas fa-level-up-alt"></i> [..]
                      </a>
                    </td>
                  </tr>
                  {% endif %}
                  {% for file in files %}
                  <tr>
                    <td class="align-middle">
                      <input type="checkbox" name="selected_files" value="{{ file.path }}">
                    </td>
                    <td class="align-middle">
                      {% if file.is_dir %}
                        <i class="fas fa-folder fa-lg text-warning"></i>
                      {% else %}
                        <i class="fas fa-file fa-lg text-secondary"></i>
                      {% endif %}
                    </td>
                    <td class="align-middle">
                      {% if file.is_dir %}
                        <a href="{{ url_for('dir_listing', req_path=file.path) }}">{{ file.name }}/</a>
                      {% else %}
                        {{ file.name }}
                      {% endif %}
                    </td>
                    <td class="align-middle">{{ file.file_type }}</td>
                    <td class="align-middle">{{ file.mtime | datetimeformat }}</td>
                    <td class="align-middle">{{ file.size | filesizeformat }}</td>
                    <td class="align-middle">
                      {% if file.is_dir %}
                        <a href="{{ url_for('dir_listing', req_path=file.path) }}" class="btn btn-sm btn-primary" title="Open Folder"><i class="fas fa-folder-open"></i></a>
                        <a href="{{ url_for('delete_folder', req_path=file.path) }}" class="btn btn-sm btn-danger" title="Delete Folder" onclick="return confirm('Are you sure you want to delete this folder (recursively)?');"><i class="fas fa-trash-alt"></i></a>
                      {% else %}
                        <a href="{{ url_for('dir_listing', req_path=file.path) }}" class="btn btn-sm btn-success" title="Download File"><i class="fas fa-download"></i></a>
                        <a href="{{ url_for('edit_file', req_path=file.path) }}" class="btn btn-sm btn-warning" title="Edit File"><i class="fas fa-edit"></i></a>
                        <a href="{{ url_for('delete_file', req_path=file.path) }}" class="btn btn-sm btn-danger" title="Delete File" onclick="return confirm('Are you sure you want to delete this file?');"><i class="fas fa-trash-alt"></i></a>
                      {% endif %}
                    </td>
                  </tr>
                  {% endfor %}
                </tbody>
              </table>
              <button type="submit" class="btn btn-danger" onclick="return confirm('Are you sure you want to delete the selected items?');">Delete Selected</button>
            </div>
          </div>
        </form>
      </div>
      
      <script>
        // Update monitoring progress bars using Bootstrap progress bars
        function updateMonitoring() {
          let sensorSelect = document.getElementById("ssd_sensor_select");
          let sensorParam = "";
          if(sensorSelect) { sensorParam = "&ssd_sensor=" + sensorSelect.value; }
          fetch("/api/monitoring?" + sensorParam)
            .then(response => response.json())
            .then(data => {
              document.getElementById("cpu-temp").textContent = data.cpu_temp;
              document.getElementById("ssd-temp").textContent = data.ssd_temp;
              let cpuBar = document.getElementById("cpuBar");
              cpuBar.style.width = data.cpu_usage + "%";
              cpuBar.textContent = data.cpu_usage + "%";
              let memBar = document.getElementById("memBar");
              memBar.style.width = data.mem_percent + "%";
              memBar.textContent = data.mem_percent + "%";
              let diskBar = document.getElementById("diskBar");
              diskBar.style.width = data.disk_percent + "%";
              diskBar.textContent = data.disk_percent + "%";
              document.getElementById("cpu-usage").textContent = data.cpu_usage;
              document.getElementById("disk-percent").textContent = data.disk_percent;
              document.getElementById("mem-text").innerHTML = data.mem_percent + "% used (" + data.mem_used_human + " / " + data.mem_total_human + ")";
            })
            .catch(err => console.error("Error fetching /api/monitoring:", err));
        }
        document.addEventListener("DOMContentLoaded", function() {
          setInterval(updateMonitoring, {{ monitor_refresh * 1000 }});
        });
        // Handle file upload with progress bar via AJAX
        document.getElementById("uploadForm").addEventListener("submit", function(e) {
          e.preventDefault();
          var form = this;
          var formData = new FormData(form);
          var xhr = new XMLHttpRequest();
          xhr.open("POST", form.action, true);
          document.getElementById("uploadProgress").style.display = "block";
          xhr.upload.addEventListener("progress", function(e) {
            if (e.lengthComputable) {
              var percentComplete = Math.round((e.loaded / e.total) * 100);
              document.getElementById("uploadProgressBar").style.width = percentComplete + "%";
              document.getElementById("uploadProgressBar").textContent = percentComplete + "%";
            }
          });
          xhr.onload = function() {
            if (xhr.status === 200) {
              alert("File(s) uploaded successfully.");
              window.location.reload();
            } else {
              alert("Error uploading file.");
            }
          };
          xhr.send(formData);
        });
      </script>
      <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.1/dist/js/bootstrap.bundle.min.js"></script>
    </body>
    </html>
    """
    return render_template_string(html_template, files=files, req_path=req_path,
                                  parent_path=parent_path, monitoring_info=monitoring_info, monitor_refresh=CONFIG["monitor_refresh"])

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=CONFIG["port"], debug=True)
