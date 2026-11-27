# Impilo Registers - local Windows setup

Runs the register API and pages on this PC against the restored Avondale database.
Everything listens on 127.0.0.1 only; nothing is reachable from other computers.

## First time (once)

1. Make sure `2026-11-02.zip` (or the extracted `Avondale_application_only.sql`) is in your
   Downloads, Documents or Desktop folder. Setup finds it automatically.
   If it is somewhere else, open a terminal in this folder and run:
   `setup.cmd -DumpPath "D:\path\to\Avondale_application_only.sql"`
2. Double-click **setup.cmd**. It will:
   - install MariaDB and Python 3.12 with winget if they are missing (Windows may ask for approval)
   - create a private database instance in `data\` on port 3307
   - import the 2.1 GB dump (10-40 minutes)
   - create the read-only `attendance_api` user, apply all grants in `sql\`, and write `api\.env`

## Every day

- **start.cmd** - starts the database, the API (http://127.0.0.1:8765) and the pages
  (http://127.0.0.1:8080), then opens your browser.
- **stop.cmd** - stops all three.
- **update.cmd** - after Claude changes the API or adds SQL scripts: applies `sql\*.sql` and restarts the API.

API docs: http://127.0.0.1:8765/docs . Logs are in `logs\`.

## Notes

- `api\.env` and `config\root-client.cnf` contain generated passwords. Don't share or commit them.
- `data\` holds patient records. Keep this folder off shared drives and out of OneDrive sync.
- Running setup again is safe: it skips the import and gives the API user a fresh password.

## Facility registry (national)

`sql\01-facility-registry.sql` builds the `registry` database from the master facility list:
`registry.province` (10), `registry.district` (69), `registry.facility_type`, and
`registry.facility_registry` (1,542 facilities: ID, name, province, district, type, latitude, longitude,
DHIS2 code). Only Avondale Clinic (ZW000A01) has clinical data in this database; every other facility
shows as registered but not yet reporting.

Facility types: Clinic, Rural / District / Provincial / Central hospital, Other hospital. The master list
has no type field, so types are derived and the source is stored per facility (`facility_type_source`):
known list of central/provincial hospitals, the facility name, or the facility code (hospital codes end in
0 + a letter). Set `facility_type_source = 'manual'` on a row to keep a hand-corrected type when the
script re-runs. Load the official MoHCC/DHIS2 classification when available.

API: `GET /api/geography`, `GET /api/facilities?province=&district=&type=&q=`, `GET /api/facilities/{id}`,
`GET /api/dashboard?province=&district=&type=&facility=&from=&to=` (`type` also accepts `HOSPITAL`).
`DATA_FACILITY_IDS` in `api\.env` (default ZW000A01) lists facilities whose data is loaded. In the future
multi-tenant database every row will carry `facility_id`, and the register queries must then filter by the
reporting facilities in scope.

The Overview tab shows `web\facility-dashboard.html`. Its source is `web-src\facility-dashboard.template.html`
plus `web-src\provinces.geoboundaries.json` (province outlines, geoBoundaries CC BY 4.0).
After editing the template, rebuild the page with `python web-src\build_dashboard.py`.

**Reporting rate** (Overview): Reporting rate (%) = facilities reported ÷ facilities expected × 100, for the selected
period, province, district, facility type, facility and report type (Attendance, OPD General, HTS, ANC, ART, TB,
Malaria, Delivery, or all). Every registered facility in the selection is expected once; it has reported when the
warehouse holds at least one record of that report type dated in the period (for "all", any report type). Each
facility counts once however many records it has. API: `GET /api/dashboard?...&report=hts` → `reportingRate`.

## Portal-wide filters

One filter bar (Province, District, Facility type, Facility, Reporting period) sits at the top of the portal
(`web\global-filters.js`) and applies to every section that shows live data: Overview and all registers.
It is remembered across sections and visits. Embedded pages hide their own filters and follow it:
the Overview via `facility-dashboard.html`, registers via `web\register-global-filters.js`.
The Overview opened on its own mounts the same bar; registers opened on their own keep their own controls.

**Multiple selection.** Province, District, Facility type and Facility are checkbox lists with search, an
"All …" row, Select All, Clear All and an "n of N selected" count. Ticks apply when the list closes (Apply,
Enter, or clicking away); Esc discards them. Nothing ticked, or everything ticked, means all. The lists cascade:
districts come from the chosen provinces, facilities from the chosen provinces, districts and types. The API
takes several IDs comma-separated (`/api/dashboard?province=ZW09,ZW04&type=CLINIC`), and access checks keep
every listed ID inside the user's area. Older pages still receive single `province`/`district`/`facility`
values (set when exactly one is chosen), so the Geospatial map follows one province.

**Reporting periods.** Today, Yesterday, This week, Last week (Monday–Sunday), Bi-weekly (two weeks from a
chosen week, default last week + this week), This month, Last month, Monthly (pick month), Quarterly (Q1 Jan–Mar …
Q4 Oct–Dec, pick quarter and year), Bi-yearly (H1 Jan–Jun / H2 Jul–Dec), Yearly, Custom period, and
"Last 1/3/6/12 months of data" (ending on the newest record; the default). Calendar periods count from today,
so they are empty until data for those dates reaches the warehouse; the bar says when a period starts after
the newest record.

## Map shading

On the Overview map, "Show on map" picks an indicator and "Shade by" picks Province, District or Facility.
Colour intensity shows the value; figures appear on hover only. District shading uses the official district
boundaries (geoBoundaries ZWE ADM2, CC BY 4.0): each facility is placed in a district by its coordinates, so
facilities without a location can't be shaded. Regenerate `web-src\districts.geoboundaries.json` with
`python web-src\build_districts.py web-src\geoBoundaries-ZWE-ADM2_simplified.geojson web-src\districts.geoboundaries.json`
(needs shapely), then rebuild `web\facility-dashboard.html` from the template.

## Geospatial intelligence

The **Geospatial intelligence** tab plays one indicator over time on the map.

- **Ask the map** in plain words, e.g. *HIV positive cases by gender from 2022 to now*, *where do HIV positive patients live?*, *ART transfers since 2023* or *where do we have most maternal deaths*. It sets the indicator, period, step, sex, movement view and place, then plays the timeline.
- **Colour and numbers** change with each period; arrows show the change from the previous period, and the three highest areas are starred ★ and glow. *Colour shows → Change* colours areas by rise or fall.
- **Sex**: show all, female only or male only, or *Compare* to colour areas by the female share and label them ♀/♂.
- **Movement**:
  - *Where patients live* draws animated lines from each patient's home suburb or town to the clinic, sized by the number of patients.
  - *Transfers* draws lines from the facility a patient transferred from (ART and TB transfer-in registers).
  - Click a suburb, town or transfer to follow it, and click it again to come back.
- **Navigation**: click a province to zoom into its districts and a district to see its facilities (circles are clinics, crosses are hospitals). Click it again, press Esc, double-click or use ↶ to go back one level. Scroll to zoom and drag to move.
- **Find a place**: search for any province, district, facility or suburb.
- **Area profile**: shows every indicator (service use, HIV, maternal health, mortality, TB and malaria) for the place in focus. Click a tile to map that indicator.
- Province and facility type follow the portal filters; the period is set on the map's timeline.
- Grey hatched areas have no data loaded yet, which is not the same as zero.

**Home addresses** are matched to places listed in `api\places.csv` (name, kind, latitude, longitude, alternative spellings). Town points come from GeoNames; suburb points are approximate centres, so please check and correct them. Edit the file and restart the API to add a place or fix a location. Today about 98% of addresses are recognised.

*Maternal deaths* counts mothers discharged as deceased after delivery plus pregnancy-related causes in the death register. Avondale has no delivery or death records yet, so it reads 0.

The page is `web\geo.html`, built from `web-src\geo.template.html` with `python web-src\build_geo.py`. Its data comes from `/api/geo/*` in `api\geo.py`.

## Users & access

Administrators manage accounts in **Users & access** (sidebar, or the menu on your name). For each person you set:

- **Role**: *Standard user*, or *Administrator* (everything, including Configuration, Users & access and the SQL editor).
- **Data access level**: *National*, *Provincial*, *District* or *Facility*, with the provinces, districts or facilities they cover. Their filter bar only offers their area, and the API refuses anything outside it.
- **Sections** they can open: Overview, Data intelligence, Geospatial intelligence, Registers, Reports, Data workspace, Facilities reporting and Ask the data. Sections they can't open disappear from their sidebar.
- **Patient details**: when off, names, ID numbers, phone numbers, addresses, next of kin, dates of birth and register numbers show as ••• in registers, register downloads and the Data workspace. Age, sex, dates and clinical details stay.
- **Downloads**: reports, registers and raw data (the Data workspace), each on or off.
- **Active**: deactivating someone signs them out at once and stops them signing in.

All of this is checked by the API on every request (`api\access.py`), so hiding a button is never the only protection. Accounts created before permissions existed keep full access until you change them.

## Data workspace

**Data workspace** shows the records behind the portal's figures: each register and the facility registry.

- **Explore**: pick a dataset and period, choose columns, add filters (contains, is, greater than, has a value, …), search, sort, and click a row to see all of it. Download as Excel or CSV if your account allows raw-data downloads (up to 200,000 rows).
- **SQL** (administrators): read-only queries against everything the portal's database account can read, with a table browser on the left. One statement at a time (SELECT, WITH, SHOW, DESCRIBE, EXPLAIN), up to 5,000 rows on screen, and queries stop after 30 seconds.

## Reports (custom report builder)

The **Reports** section lets anyone build their own report.

- **Build:** start from scratch or from a template (HIV summary, Maternal health, Monthly service use, TB and malaria). Each report has a title, description, period (from, to and step), place (province, district, facility type, facility), sex, and any number of sections.
- **Sections:** a *table or chart* section takes up to 8 indicators. Choose what the rows and columns are grouped by (period, province, district, facility, facility type, sex or age group) and show it as a table, bar, line or pie chart, with the table underneath if you like. A *text* section holds notes and findings. Sections can be reordered or removed.
- **Ask in words:** type e.g. *HIV positive cases by province and sex for 2024 as a bar chart* and a matching section is added.
- **Save and share:** reports are private to their creator until *Share with everyone* is switched on. Everyone else can then open the report read-only and *Duplicate* it to make their own copy. Only the creator can change a report; the creator or an administrator can delete it.
- **Download:** *Excel* (tables with Excel charts), *PDF* (print-ready, with the ministry header) or *CSV*. Unsaved reports can be downloaded too.
- Empty cells mean no facility in that area has data loaded yet, which is not the same as zero.

Saved reports live in `api\report-data\reports.json`. That folder holds users' work: keep it with backups and out of GitHub (it's in `.gitignore`).

## AI assistant (Ollama)

"Ask the data" is the chat: answers use aggregated portal figures for the current filters (never patient records).
Models run on this PC with Ollama (https://ollama.com/download). The **Configuration** section (below "Ask the data") has:
- **Models** - start/stop Ollama, download models (with progress), start/stop a model in memory, delete, set the default.
- **Optimise** - creativity, answer length, reading window, memory, CPU/GPU use; "Recommend for this PC".
- **Train** - assistants with a domain, instructions, answer style, language and example Q&As. "Build" creates a
  custom Ollama model (`impilo-<name>`). This shapes answers; it does not fine-tune the model's weights.

Code: `api\ai.py` (endpoints under /api/ai), `web\ai.html`. Settings and assistants are saved in `api\ai-data\`.
Ollama address: `OLLAMA_URL` in `api\.env` (default http://127.0.0.1:11434).


**Charts in the chat.** Ask for a graph, or for a breakdown, and the answer includes a chart. For example: *graph HIV positive cases by age for females and males*, *chart OPD visits by month*, *malaria cases by age as a pie chart*. The figures are worked out by the portal itself (`api\charts.py`, the same engine as Reports), so they're exact; the assistant only describes them. Each chart has **Show figures** (the table) and **Download image** (PNG). Follow-ups like *now by year* reuse the last question's indicator. If the assistant model is offline, the chart still appears.

## Sign-in and users

The portal now requires a sign-in. The first time you open it you'll see **Set up the portal**: create the first administrator there.

- Administrators can add, edit and deactivate users from the menu on their name (top right), and are the only ones who can change the AI configuration.
- Viewers can use every page and chat with the assistant.
- Accounts live in `api/auth-data/users.json` (passwords are stored only as salted hashes) and sign-ins are signed with `api/auth-data/secret.key`. Keep that folder private and don't copy it to other machines. Deleting `secret.key` signs everyone out.
- If every administrator is locked out, stop the portal, delete `api/auth-data/users.json` and start again to get the setup screen back.
- Sessions last 12 hours. Five wrong passwords in 15 minutes pause sign-in for that account.

Saved chats are kept per user in the browser, and any chat can be starred to pin it under **Favourites**.
