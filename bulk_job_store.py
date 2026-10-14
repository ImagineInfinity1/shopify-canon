"""Transactional, per-item bulk-job checkpoints in the application's database."""
import base64
from datetime import datetime
import json
import zlib

from extensions import db

ITEM_KINDS = ('products', 'enhancements', 'failures', 'apply_results')


class BulkJobRecord(db.Model):
    __tablename__ = 'bulk_job_records'
    id = db.Column(db.String(64), primary_key=True)
    user_id = db.Column(db.String, nullable=False, index=True)
    metadata_json = db.Column(db.Text, nullable=False)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)


class BulkJobItem(db.Model):
    __tablename__ = 'bulk_job_items'
    job_id = db.Column(db.String(64), db.ForeignKey('bulk_job_records.id', ondelete='CASCADE'), primary_key=True)
    kind = db.Column(db.String(20), primary_key=True)
    position = db.Column(db.Integer, primary_key=True)
    payload = db.Column(db.Text, nullable=False)


def encode(value):
    return base64.b64encode(zlib.compress(json.dumps(value, ensure_ascii=False, default=str).encode('utf-8'))).decode('ascii')


def decode(value):
    return json.loads(zlib.decompress(base64.b64decode(value)).decode('utf-8'))


def save_snapshot(job_id, metadata, batches, reset=False):
    """Commit metadata and only the new/changed item rows in one transaction."""
    record = db.session.get(BulkJobRecord, job_id)
    if record is None:
        record = BulkJobRecord(id=job_id, user_id=str(metadata['user_id']))
        db.session.add(record)
    elif record.user_id != str(metadata['user_id']):
        raise ValueError('Bulk job ownership cannot change')
    record.metadata_json = json.dumps(metadata, ensure_ascii=False, default=str)
    record.updated_at = datetime.utcnow()
    if reset:
        BulkJobItem.query.filter_by(job_id=job_id).delete(synchronize_session=False)
    db.session.flush()
    for kind, entries in batches.items():
        if kind not in ITEM_KINDS:
            raise ValueError('Unknown bulk job item kind')
        for position, value in entries:
            db.session.merge(BulkJobItem(job_id=job_id, kind=kind, position=position, payload=encode(value)))
    db.session.commit()


def load_job(job_id, user_id):
    record = BulkJobRecord.query.filter_by(id=job_id, user_id=str(user_id)).first()
    if record is None:
        return None
    job = json.loads(record.metadata_json)
    for kind in ITEM_KINDS:
        job[kind] = []
    for item in BulkJobItem.query.filter_by(job_id=job_id).order_by(BulkJobItem.kind, BulkJobItem.position):
        if item.position != len(job[item.kind]):
            raise RuntimeError('Bulk job checkpoint is incomplete; refusing to resume')
        job[item.kind].append(decode(item.payload))
    job['_persisted_counts'] = {kind: len(job[kind]) for kind in ITEM_KINDS}
    return job


def pause_interrupted_jobs():
    # Runtime is configured for one web worker. Multi-instance execution needs
    # distributed leases before it can be enabled safely.
    for record in BulkJobRecord.query.yield_per(100):
        job = json.loads(record.metadata_json)
        if job.get('status') not in ('queued', 'processing', 'applying'):
            continue
        job['phase'] = job.get('phase') or ('apply' if job['status'] == 'applying' else 'enhance')
        job['status'] = 'paused'
        job['current_step'] = ('Interrupted during a Shopify write. Refresh and reconcile that product before retrying.'
                               if job.get('in_flight') else 'Paused after restart. Resume to continue.')
        record.metadata_json = json.dumps(job, ensure_ascii=False)
    db.session.commit()
