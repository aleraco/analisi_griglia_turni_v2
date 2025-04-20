import os
import re
import uuid
import time
import pdfplumber
import pandas as pd
import threading
from flask import Flask, render_template, request, send_file, session, redirect, url_for
from ics import Calendar, Event
from datetime import datetime, timedelta
import pytz
from calendar import monthrange
import hashlib

# Configurazione applicazione
app = Flask(__name__)
app.secret_key = "supersecretkey_prod_123!@#"

# Cartelle di lavoro
UPLOAD_FOLDER = "uploads"
ICS_FOLDER = "calendars"
DB_FOLDER = "database"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(ICS_FOLDER, exist_ok=True)
os.makedirs(DB_FOLDER, exist_ok=True)

# Timezone e storage temporaneo
TZ = pytz.timezone("Europe/Rome")
TEMPORARY_STORAGE = {}
CLEANUP_INTERVAL = 3600  # 1 ora

# Pulizia storage
def storage_cleanup():
    while True:
        time.sleep(CLEANUP_INTERVAL)
        now = time.time()
        expired = [k for k, v in TEMPORARY_STORAGE.items() if now - v['timestamp'] > CLEANUP_INTERVAL * 2]
        for key in expired:
            del TEMPORARY_STORAGE[key]

cleanup_thread = threading.Thread(target=storage_cleanup)
cleanup_thread.daemon = True
cleanup_thread.start()

# Calcolo hash file
def file_hash(filepath, block_size=65536):
    hasher = hashlib.md5()
    with open(filepath, 'rb') as f:
        for block in iter(lambda: f.read(block_size), b''):
            hasher.update(block)
    return hasher.hexdigest()

# Estrazione mese e anno
def extract_month_year_from_table(df):
    for cell in df.iloc[0]:
        if isinstance(cell, str):
            match = re.search(r"(gen|feb|mar|apr|mag|giu|lug|ago|set|ott|nov|dic)[a-zA-Z]*[-_\s]?(\d{2,4})", cell, re.IGNORECASE)
            if match:
                mese_abbr = match.group(1).lower()
                anno = match.group(2)
                if len(anno) == 2:
                    anno = "20" + anno
                months_map = {
                    "gen": "January", "feb": "February", "mar": "March",
                    "apr": "April", "mag": "May", "giu": "June",
                    "lug": "July", "ago": "August", "set": "September",
                    "ott": "October", "nov": "November", "dic": "December"
                }
                if mese_abbr in months_map:
                    return months_map[mese_abbr], int(anno)
    return None, None

# Estrazione tabella
def extract_table_from_pdf(pdf_path):
    tables = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            table = page.extract_table()
            if table:
                df = pd.DataFrame(table).dropna(axis=0, how="all").dropna(axis=1, how="all")
                tables.append(df)
    return pd.concat(tables, ignore_index=True) if tables else None

# Traduzione turni
def translate_shifts(df, month, year):
    work_hours = {"d": 4, "e": 5, "f": 6, "g": 7, "h": 8}
    special_events = {"R1", "R2", "FER", "R0", "OFF", "FEST"}
    month_number = datetime.strptime(month, "%B").month
    _, num_days = monthrange(year, month_number)
    days = list(range(1, num_days + 1))
    pivot_data = {}

    for idx, row in df.iterrows():
        if idx == 0:
            continue
        nome_cell = row.iloc[0]
        if pd.isna(nome_cell) or not isinstance(nome_cell, str):
            continue
        nome = nome_cell.split(",")[0].strip()
        if nome not in pivot_data:
            pivot_data[nome] = {str(day): "" for day in days}

        for day in days:
            if day >= len(row):
                continue
            raw_value = str(row.iloc[day]).strip().lower() if (day < len(row) and not pd.isna(row.iloc[day])) else ""
            clean_value = re.sub(r'[^a-z0-9]', '', raw_value)
            if not clean_value:
                continue
            if clean_value in special_events:
                pivot_data[nome][str(day)] = clean_value.upper()
            elif len(clean_value) >= 2 and clean_value[0] in work_hours:
                prefix = clean_value[0]
                number_part = re.sub(r'[^0-9]', '', clean_value[1:])
                if number_part:
                    try:
                        start_code = int(number_part)
                        duration = work_hours[prefix]
                        start_h = start_code / 2
                        start_time = f"{int(start_h):02d}:{'00' if start_h % 1 == 0 else '30'}"
                        pivot_data[nome][str(day)] = f"{start_time} ({duration})"
                    except:
                        pivot_data[nome][str(day)] = "Formato non valido"
                else:
                    pivot_data[nome][str(day)] = clean_value.upper()
            else:
                pivot_data[nome][str(day)] = clean_value.upper()

    translated_df = pd.DataFrame.from_dict(pivot_data, orient="index")
    translated_df.columns.name = "Giorno"
    translated_df.index.name = "Nome"
    translated_df.reset_index(inplace=True)
    cols = ["Nome"] + [str(day) for day in days]
    return translated_df[cols].fillna("")

# Generazione file ICS

# Generazione file ICS

def generate_ics_files(translated_df, month, year):
    ics_files = {}
    month_number = datetime.strptime(month, "%B").month
    special_events = {"R1", "R2", "FER", "R0", "OFF", "FEST"}
    for nome in translated_df["Nome"].unique():
        cal = Calendar()
        cal.creator = "Analisi Griglia PDF"
        cal.timezone = TZ.zone
        person_shifts = translated_df[translated_df["Nome"] == nome]
        for _, row in person_shifts.iterrows():
            for day_str in [c for c in translated_df.columns if c != "Nome" and c.isdigit()]:
                day = int(day_str)
                value = row[day_str]
                if not value or pd.isna(value):
                    continue
                try:
                    naive_date = datetime(year, month_number, day)
                    aware_date = TZ.localize(naive_date)
                    if value in special_events:
                        event = Event(name=value, begin=aware_date.replace(hour=0, minute=1), end=aware_date.replace(hour=23, minute=59))
                    elif "(" in value and ")" in value:
                        start_time, duration = value.split(" (")
                        duration = int(duration.replace(")", ""))
                        start_h, start_m = map(int, start_time.split(":"))
                        begin = aware_date.replace(hour=start_h, minute=start_m)
                        event = Event(name=f"Turno: {start_time} ({duration}h)", begin=begin, end=begin + timedelta(hours=duration))
                    cal.events.add(event)
                except Exception as e:
                    print(f"Errore creazione evento: {str(e)}")
        nome_file = f"{nome.replace(' ', '_')}_{month.lower()}.ics"
        file_path = os.path.join(ICS_FOLDER, f"{nome.lower()}_{month.lower()}.ics")
        
        with open(file_path, "w") as f:
            f.writelines(cal.serialize_iter())
        ics_files[nome] = file_path
    return ics_files

# Pagina principale
@app.route("/", methods=["GET", "POST"])
def upload():
    if request.method == "POST":
        # Accesso a database esistente con nome utente
        if "access_db" in request.form:
            mese = request.form.get("mese")
            anno = request.form.get("anno")
            nome = request.form.get("nome", "").strip()

            folder = os.path.join(DB_FOLDER, f"{mese}-{anno}")
            df_path = os.path.join(folder, "dataframe_tradotto.csv")
            parquet_path = os.path.join(folder, "storico.parquet")
            original_csv_path = os.path.join(folder, "dataframe_originale.csv")

            
             # Verifica esistenza dei file
            if not (os.path.exists(df_path) and os.path.exists(parquet_path) and os.path.exists(original_csv_path)):
                return "Database non trovato o incompleto"
            
            # Carica i dati
            df_originale = pd.read_csv(original_csv_path)
            df_tradotto = pd.read_parquet(parquet_path)

            # Verifica nome utente
            nomi_presenti = df_tradotto["Nome"].str.strip().str.lower().unique()
            if nome.lower() not in nomi_presenti:
                return render_template("result_personale.html", error="Non sei autorizzato ad accedere ai dati", utente=nome)

            # Estrai la riga originale e tradotta
            riga_orig = df_originale[df_originale.iloc[:, 0].str.strip().str.lower() == nome.lower()]
            riga_trad = df_tradotto[df_tradotto["Nome"].str.strip().str.lower() == nome.lower()]

            originale = riga_orig.to_html(classes='table table-sm table-bordered', index=False, border=0)
            tradotta = riga_trad.to_html(classes='table table-sm table-bordered', index=False, border=0)

            return render_template("result_personale.html",
                                   utente=nome,
                                   originale=originale,
                                   tradotta=tradotta,
                                   mese=mese,
                                   anno=anno)

        # Caricamento nuovo PDF
        file = request.files["file"]
        if not file or file.filename == '':
            return "Nessun file selezionato"
        pdf_path = os.path.join(UPLOAD_FOLDER, file.filename)
        file.save(pdf_path)

        try:
            df_originale = extract_table_from_pdf(pdf_path)
            if df_originale is None or df_originale.empty:
                return "Nessuna tabella trovata nel PDF"

            mese, anno = extract_month_year_from_table(df_originale)
            if not mese or not anno:
                return "Impossibile determinare mese/anno"

            translated_df = translate_shifts(df_originale, mese, anno)
            pdf_hash = file_hash(pdf_path)
            month_folder = os.path.join(DB_FOLDER, f"{mese}-{anno}")
            os.makedirs(month_folder, exist_ok=True)

            new_pdf_path = os.path.join(month_folder, f"originale_{pdf_hash}.pdf")
            if not os.path.exists(new_pdf_path):
                os.rename(pdf_path, new_pdf_path)
            else:
                os.remove(pdf_path)

            translated_df.to_csv(os.path.join(month_folder, "dataframe_tradotto.csv"), index=False)
            df_originale.to_csv(os.path.join(month_folder, "dataframe_originale.csv"), index=False)

            db_path = os.path.join(month_folder, "storico.parquet")
            translated_df["__HASH__"] = pdf_hash
            if os.path.exists(db_path):
                existing_df = pd.read_parquet(db_path)
                if pdf_hash not in existing_df["__HASH__"].values:
                    updated_df = pd.concat([existing_df, translated_df], ignore_index=True)
                    updated_df.to_parquet(db_path, index=False)
            else:
                translated_df.to_parquet(db_path, index=False)

            ics_files = generate_ics_files(translated_df, mese, anno)

            return render_template("result.html",
                                   tabella_originale=df_originale.to_html(classes='table table-sm table-bordered', index=False, border=0),
                                   tabella_tradotta=translated_df.to_html(classes='table table-sm table-bordered', index=False, border=0),
                                   mese=mese,
                                   anno=anno)

        except Exception as e:
            return f"Errore durante l'elaborazione: {str(e)}"

    # GET: pagina iniziale
    mesi_disponibili = []
    if os.path.exists(DB_FOLDER):
        mesi_disponibili = sorted(
            [(m, len(pd.read_parquet(os.path.join(DB_FOLDER, m, 'storico.parquet'))))
             for m in os.listdir(DB_FOLDER)
             if os.path.exists(os.path.join(DB_FOLDER, m, 'storico.parquet'))],
            key=lambda x: x[0], reverse=True
        )
    return render_template("upload.html", mesi=mesi_disponibili)

@app.route("/download/<nome>/<mese>")
def download_ics_personale(nome, mese):
    filename = f"{nome.lower()}_{mese.lower()}.ics"
    file_path = os.path.join(ICS_FOLDER, filename)

    if not os.path.exists(file_path):
        return f"File {filename} non trovato nella cartella calendars.", 404

    return send_file(file_path, as_attachment=True)


@app.route("/result")
def result():
    session_id = session.get('current_session')
    if not session_id or session_id not in TEMPORARY_STORAGE:
        return redirect(url_for("upload"))
    data = TEMPORARY_STORAGE[session_id]
    return render_template("result.html",
                           original_table=data['original_table'],
                           translated_table=data['translated_table'],
                           ics_files=data['ics_files'],
                           mese=data['mese'],
                           anno=data['anno'])

@app.route("/cambio-turno", methods=["GET", "POST"])
def cambio_turno():
    if request.method == "POST":
        giorno = request.form.get("giorno")
        orario = request.form.get("orario")
        durata = request.form.get("durata")

        try:
            data = datetime.strptime(giorno, "%Y-%m-%d")
            giorno_col = str(data.day)  # le colonne nel parquet sono '1', '2', ...
            mese = data.strftime("%B")
            anno = str(data.year)
            cartella = os.path.join(DB_FOLDER, f"{mese}-{anno}")
            path_parquet = os.path.join(cartella, "storico.parquet")

            if not os.path.exists(path_parquet):
                return render_template("cambio_turno.html", messaggio="Nessun database disponibile per quel mese.")

            df = pd.read_parquet(path_parquet)

            if giorno_col not in df.columns:
                return render_template("cambio_turno.html", messaggio=f"Giorno {giorno_col} non presente nei dati.")

            valore_turno = f"{orario} ({durata})"
            disponibili = df[df[giorno_col] == valore_turno]["Nome"].tolist()

            if disponibili:
                messaggio = f"Sono disponibili per cambio turno: {', '.join(disponibili)}"
            else:
                messaggio = "Nessuno ha un turno compatibile per il cambio richiesto."

            return render_template("cambio_turno.html", messaggio=messaggio)

        except Exception as e:
            return render_template("cambio_turno.html", messaggio=f"Errore: {str(e)}")

    return render_template("cambio_turno.html")
 
@app.route("/result-personale", methods=["GET"])
def result_personale():
    nome = request.args.get("nome", "").strip()
    mese = request.args.get("mese", "")
    anno = request.args.get("anno", "")

    if not nome or not mese or not anno:
        return "Parametri mancanti"

    folder = os.path.join(DB_FOLDER, f"{mese}-{anno}")
    parquet_path = os.path.join(folder, "storico.parquet")

    if not os.path.exists(parquet_path):
        return "Database non trovato"

    df_tradotto = pd.read_parquet(parquet_path)

    nome_norm = nome.strip().upper()
    if nome_norm not in df_tradotto["Nome"].str.strip().str.upper().unique():
        return render_template("result_personale.html", error="Non sei autorizzato ad accedere ai dati", utente=nome)

    riga_trad = df_tradotto[df_tradotto["Nome"].str.strip().str.upper() == nome_norm]
    tradotta = riga_trad.to_html(classes="table table-sm table-bordered", index=False, border=0)

    return render_template("result_personale.html",
                           utente=nome,
                           tradotta=tradotta,
                           mese=mese,
                           anno=anno)

 
if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000, debug=False)
