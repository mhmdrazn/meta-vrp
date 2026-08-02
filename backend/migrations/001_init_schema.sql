-- Skema awal lengkap untuk Meta-VRP di database Postgres baru (mis. Supabase).
-- Idempotent (aman dijalankan berulang) via IF NOT EXISTS.
-- Jalankan file ini SEBELUM schema_additions.sql pada project database yang benar-benar kosong.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- === Ringkasan per kendaraan untuk satu job optimasi ===
CREATE TABLE IF NOT EXISTS vrp_job_vehicle_runs (
  job_id UUID NOT NULL,
  vehicle_id INT NOT NULL,
  route_total_time_min NUMERIC,
  expected_finish_local TIMESTAMP,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (job_id, vehicle_id)
);

CREATE INDEX IF NOT EXISTS idx_vrp_job_vehicle_runs_job ON vrp_job_vehicle_runs(job_id);

-- === Langkah rute per kendaraan (dibuat oleh /optimize) ===
CREATE TABLE IF NOT EXISTS vrp_job_step_status (
  job_id UUID NOT NULL,
  vehicle_id INT NOT NULL,
  sequence_index INT NOT NULL,
  node_id TEXT,
  status TEXT NOT NULL,
  reason TEXT,
  ts TIMESTAMPTZ NOT NULL DEFAULT now(),
  author TEXT,
  PRIMARY KEY (job_id, vehicle_id, sequence_index)
);

CREATE INDEX IF NOT EXISTS idx_step_status_jobveh ON vrp_job_step_status(job_id, vehicle_id);
