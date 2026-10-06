# Run Registry — Workflow Operasional Bulanan (Indonesia)

Sistem **Run Registry** di HydroHub digunakan untuk mengatur *pengoperasian* berkelanjutan simulasi *hydrological model* (Wflow) dengan *state continuity* (lanjut dari simulasi sebelumnya). Sistem ini otomatis menangani *warm start* (melanjutkan dari state sebelumnya), *run ID*, serta *config hash* untuk mengontrol kapan *chain* run dapat terus berlanjut.

---

## 1. Konsep Inti

| Komponen | Penjelasan |
|---|---|
| **RunRecord** | Satu baris riwayat di `registry.json`. Menyimpan `run_id`, tanggal mulai/selesai, `config_hash`, `status`, serta path state input/output. |
| **RunPlan** | Rencana run sebelum dieksekusi. Berisi `predecessor`, `config_hash`, `start_mode`, serta peringatan (`warnings`) atau error (`errors`). |
| **RunChain** | Susunan run *completed* yang bisa *warm-start* satu sama lain. Deteksi via `registry.chains(timestep_seconds)`. |
| **config_hash** | SHA-256 dari `wflow_sbm.toml`, tetapi **mengecualikan** field-field dinamis yang berubah setiap run (`time`, `path_forcing`, `path_input`, `path_output`). Hash hanya "terpengaruh" oleh data statis (grid, parameter). |

---

## 2. Apa yang MENGUBAH Hash vs TIDAK

| Kategori | Field di `wflow_sbm.toml` | Mempengaruhi `config_hash`? |
|---|---|---|
| **Statis (ubah = *chain* putus)** | `input.path_static`, `model.*`, parameter numerik, `output.*format`, `output.*encoding` | ✅ **YA** — ubah sembarang field statis, *chain* akan terputus, perlu *cold start*. |
| **Dinamis per run (boleh berubah)** | `time.starttime`, `time.endtime` | ❌ **TIDAK** — di-*pop* saat menghitung hash. |
| | `state.path_input`, `state.path_output` | ❌ **TIDAK** — di-*pop*. |
| | `input.path_forcing` | ❌ **TIDAK** — di-*pop*. |
| | `dir_output`, `output.*path` | ❌ **TIDAK** — di-*pop*. |

**Kesimpulan:** Setiap bulan, hanya perlu memperbaharui `forcing.nc` dan rentang waktu (`starttime`/`endtime`). Hash akan tetap sama selama grid/parameter tidak disentuh.

---

## 3. Alur Operasional Bulanan

```text
1. HydroMT update
   → Memasukkan data forcing baru (bulan berikutnya).
   → File `forcing.nc` diperbarui.

2. Registry plan
   → Panggil `registry.plan(start="2025-01-01", end="2025-01-31", timestep_seconds=86400, start_mode="auto")`.
   → Sistem akan otomatis cari *predecessor* (run terakhir *completed*).
   → Jika *predecessor* ada dan `config_hash` sama → `plan.errors` **kosong** → *warm start* diizinkan.
   → Jika `config_hash` beda (grid/param berubah) → *error* minta *cold start*.

3. Registry prepare_run
   → Panggil `registry.prepare_run(plan)`.
   → Sistem otomatis:
      • Buat direktori `runs/<run_id>/`.
      • Tulis `config.toml` di direktori run dengan `starttime`, `endtime`, `state.path_input` → `outstate.nc` lama, `state.path_output` → `outstate.nc` baru.
      • Simpan *record* ke `registry.json`.

4. Eksekusi Julia
   → Jalankan `julia run_wflow.jl` (atau perintah sesuai konfigurasi).
   → Model menghitung dari `state` lama menuju `state` baru.

5. Registry finish_run
   → Setelah simulasi selesai, panggil `registry.finish_run(run_id, success=True)`.
   → Status run di `registry.json` berubah jadi `"completed"`.
   → Run ini sekarang siapa dijadikan *predecessor* untuk run berikutnya.

6. Ulangi
   → Bulan berikutnya, steps 1–5 diulang.
```

---

## 4. Contoh Kode Python

### Inisialisasi dan Planning

```python
from pathlib import Path
from hydrohub.core.run_registry import RunRegistry

# Inisialisasi registry (butuh project_root dan model_root)
registry = RunRegistry(
    project_root=Path("/path/to/project"),
    model_root=Path("/path/to/model"),
)

# Planning: auto cold/warm start
plan = registry.plan(
    start=datetime(2025, 1, 1),
    end=datetime(2025, 1, 31),
    timestep_seconds=86400,       # 1 hari
    run_id="run_202501_202501",
    start_mode="auto",            # cari predecessor otomatis
)

if plan.errors:
    print("❌ Planning gagal, cek error:", plan.errors)
elif plan.warnings:
    print("⚠️  Peringatan:", plan.warnings)
else:
    print("✅ Planning OK, config_hash:", plan.config_hash)
```

### Prepare & Finish Run

```python
# Prepare run (otomatis buat config.toml di run_dir)
record, config_path = registry.prepare_run(plan)
print(f"✅ Run dir: {record.run_id}, config: {config_path}")

# ... jalankan simulasi Julia di sini ...

# Finish run (update status jadi "completed")
finished = registry.finish_run(record.run_id, success=True)
print(f"✅ Selesai, status: {finished.status}")
```

### Deteksi Rantai & Masalah

```python
# Lihat rantai run yang valid
chains = registry.chains(timestep_seconds=86400)
for chain in chains:
    if chain.reason is None:
        print(f"✅ Rantai valid: {chain.label}")
    else:
        print(f"⚠️  Rantai putus: {chain.label} → {chain.reason}")

# Cek detail masing-masing run
for run in registry.list_runs():
    print(f"  {run.run_id}: {run.start.date()} → {run.end.date()}, status={run.status}, hash={run.config_hash[:8]}...")
```

---

## 5. Troubleshooting Umum

| Masalah | Penyebab | Solusi |
|---|---|---|
| `"predecessor config does not match"` | `config_hash` berbeda (grid/parameter di `wflow_sbm.toml` diubah). | Gunakan `start_mode="cold"` atau kembali ke config hash lama. |
| `"Warm start state is missing"` | File `outstate.nc` dari run sebelumnya tidak ditemukan. | Periksa `state_input_path` di config run baru, atau pastikan run sebelumnya `finish_run`-nya sukses. |
| `"gap"` di `registry.chains()` | `run.end` tidak persis `start - timestep` (off per detik). | Cek `endtime` di `config.toml` run sebelumnya, pastikan sesuai dengan interval model. |
| `"forcing source was revised"` | File `forcing.nc` diubah (ukuran/fingerprint berubah). | Normal jika update forcing baru. Chain tetap jalan selama `config_hash` sama. |
| Run ID sudah ada | Mencoba overwrite run yang sedang digunakan. | Ganti `run_id` atau centang opsi overwrite di UI dengan *backup* pilihan. |

---

## 6. Catatan Teknis

- **Hash hanya "statis":** Karena `config_hash()` mengecualikan `time`, `path_forcing`, `path_input`, `path_output`, berarti Anda bisa memperbarui forcing & tanggal setiap bulan tanpa perlu *cold start*.
- **`predecessor()` check ketat:** Sistem memerlukan `run.end == start - timestep` **persis**. Jika `endtime` run sebelumnya `2024-12-31T23:00:00` dan `starttime` run selanjutnya `2025-01-01T00:00:00`, *gap* 1 jam → *chain putus* → butuh *cold start*.
- **State continuity:** Run baru akan meng-*load* `state_output_path` dari run sebelumnya sebagai `state_input_path`. Pastikan file `outstate.nc` dari run sebelumnya tersedia dan tidak rusak.
- **Finish wajib:** `registry.finish_run()` **harus** dipanggil setelah simulasi sukses, baru run tersebut bisa dijadikan *predecessor*. Jika *fail*, status jadi `"failed"` dan tidak bisa melanjutkan rantai.

---

## 7. Ringkian CLI / UI

- **Di UI HydroHub** (workflow_pages.py), tombol "Run" akan secara otomatis menjalankan `plan()` → `prepare_run()` → proses Julia → `finish_run()`.
- **Cek registry:** Di bagian *Run History* di UI, bisa melihat daftar `run_id`, status, serta tombol *export* output.
- **Export gabungan:** Fungsi `registry.build_combined_output(chain)` dapat menggabungkan output CSV/NC dari rantai runs untuk analisis bulanan.

---

**Selesai.** Simpan file ini di `docs/run_registry_workflow.md` untuk referensi operasional tim.