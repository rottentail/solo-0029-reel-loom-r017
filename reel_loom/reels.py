import copy
import json
from .errors import LoomError, require, missing, revision_conflict
from .timebase import range_ticks, seconds, ticks, TICKS_PER_SECOND
from .values import identifier, text, token, now, canonical, digest, object_body, integer

EDIT_FIELDS = ("slice", "in", "out", "position", "label")


class Reels:
    def __init__(self, db, assets, keys, settings):
        self.db, self.assets, self.keys, self.settings = db, assets, keys, settings

    def normalize(self, body, db):
        object_body(body, ("title", "slices"), ("title", "slices"))
        title = text(body["title"], "title")
        require(isinstance(body["slices"], list) and 1 <= len(body["slices"]) <= 64,
                "slice_count", "A reel requires 1..64 slices")
        slices, cursor = [], 0
        for raw in body["slices"]:
            object_body(raw, ("asset", "in", "out", "label"), ("asset", "in", "out"))
            asset = self.assets.get(raw["asset"], db, ready=True)
            a, b = range_ticks(raw["in"], raw["out"], asset["metadata"]["duration_tick"])
            slices.append({"id": identifier("slice"), "asset": asset["id"],
                           "in_tick": a, "out_tick": b, "at_tick": cursor,
                           "label": text(raw.get("label", asset["label"]), "slice label")})
            cursor += b-a
        require(cursor <= self.settings.max_seconds * TICKS_PER_SECOND,
                "reel_duration", "Reel exceeds service duration limit")
        return {"title": title, "slices": slices, "duration_tick": cursor}

    def create(self, body, key=None):
        with self.db.write_unit() as db:
            previous = self.keys.replay(db, "reels", key, body)
            if previous:
                return previous
            document = self.normalize(body, db)
            reel_id = identifier("reel")
            db.execute("INSERT INTO reels VALUES(?,?,?,?,?)", (reel_id, document["title"], 1, "open", now()))
            self._write_revision(db, reel_id, 1, document)
            result = self.get(reel_id, db=db)
            return self.keys.record(db, "reels", key, body, result)

    def _write_revision(self, db, reel_id, revision, document):
        db.execute("INSERT INTO revisions VALUES(?,?,?,?,?)",
                   (reel_id, revision, canonical(document), digest(document), now()))
        self.db.event(db, reel_id, "revision_written", {"revision": revision, "hash": digest(document)})

    def replace(self, reel_id, expected, body):
        integer(expected, "expected_revision", 1)
        with self.db.write_unit() as db:
            current = self.get(reel_id, db=db)
            require(current["state"] == "open", "reel_sealed", "Sealed reels cannot be edited", 409)
            if current["head"] != expected:
                raise revision_conflict(expected, current["head"])
            document = self.normalize(body, db)
            revision = expected + 1
            self._write_revision(db, reel_id, revision, document)
            db.execute("UPDATE reels SET head=?,title=? WHERE id=?", (revision, document["title"], reel_id))
            return self.get(reel_id, db=db)

    def patch(self, reel_id, expected, body):
        """Edit existing slices in place; untouched slice identities are preserved."""
        integer(expected, "expected_revision", 1)
        object_body(body, ("edits",), ("edits",))
        require(isinstance(body["edits"], list) and 1 <= len(body["edits"]) <= 64,
                "edit_count", "A slice edit requires 1..64 edits")
        with self.db.write_unit() as db:
            current = self.get(reel_id, db=db)
            require(current["state"] == "open", "reel_sealed", "Sealed reels cannot be edited", 409)
            require(expected <= current["head"], "revision_conflict",
                    "The supplied revision is newer than the reel head", 409,
                    **{"expected": expected, "current": current["head"]})
            if expected == current["head"]:
                edits = self._prepare_edits(current["document"], body["edits"], db)
                kept, merged_from = [self._edit_request(edit) for edit in edits], None
            else:
                base = self.get(reel_id, expected, db=db)
                requested = self._prepare_edits(base["document"], body["edits"], db)
                keep, reconfirm = self._reconcile(base["document"], current["document"], requested)
                if reconfirm:
                    raise LoomError(
                        "revision_conflict", "The reel has changed; reconfirm conflicting edits", 409,
                        {"expected": expected, "current": current["head"],
                         "kept": [self._edit_request(edit) for edit in keep],
                         "reconfirm": reconfirm,
                         "hint": "Nothing was written. Resubmit the kept edits with the current If-Match "
                                 "after resolving the reconfirm entries."})
                kept = [self._edit_request(edit) for edit in keep]
                edits = self._prepare_edits(current["document"], kept, db)
                merged_from = expected
            # A new revision is a new arrangement: like a full replacement it may only
            # reference ready sources, while frozen tickets keep reading retired ones.
            for asset_id in {item["asset"] for item in current["document"]["slices"]}:
                self.assets.get(asset_id, db, ready=True)
            document = self._apply_edits(current["document"], edits)
            revision = current["head"] + 1
            self._write_revision(db, reel_id, revision, document)
            db.execute("UPDATE reels SET head=?,title=? WHERE id=?", (revision, document["title"], reel_id))
            self.db.event(db, reel_id, "slices_edited",
                          {"revision": revision, "base": expected, "merged_from": merged_from,
                           "kept": kept, "edits": [self._edit_request(edit) for edit in edits]})
            result = self.get(reel_id, db=db)
            if merged_from is not None:
                result = {**result, "merged": True, "merged_from": merged_from, "kept": kept}
            return result

    def patch_slice(self, reel_id, slice_id, expected, body):
        object_body(body, ("in", "out", "position", "label"), ())
        return self.patch(reel_id, expected, {"edits": [{**body, "slice": slice_id}]})

    def _prepare_edits(self, document, raw_edits, db):
        """Validate every edit against ``document`` without mutating anything."""
        by_id = {item["id"]: index for index, item in enumerate(document["slices"])}
        seen, edits = set(), []
        for raw in raw_edits:
            object_body(raw, EDIT_FIELDS, ("slice",))
            slice_id = token(raw["slice"], "slice")
            require(slice_id not in seen, "duplicate_edit",
                    "A slice can only be edited once per request", slice=slice_id)
            seen.add(slice_id)
            require(slice_id in by_id, "slice_not_found", "Slice does not exist in this revision", 404,
                    slice=slice_id)
            require(any(field in raw for field in ("in", "out", "position", "label")),
                    "empty_edit", "An edit must change in, out, position, or label", slice=slice_id)
            item = document["slices"][by_id[slice_id]]
            edit = {"slice": slice_id, "index": by_id[slice_id]}
            if "in" in raw or "out" in raw:
                # The slice already references this source; a trim does not add a new reference.
                asset = self.assets.get(item["asset"], db)
                start = ticks(raw["in"], "in") if "in" in raw else item["in_tick"]
                end = ticks(raw["out"], "out") if "out" in raw else item["out_tick"]
                require(start < end, "empty_slice", "A slice must have positive length", slice=slice_id)
                require(end <= asset["metadata"]["duration_tick"], "slice_bounds",
                        "Slice extends beyond source media", slice=slice_id,
                        duration=seconds(asset["metadata"]["duration_tick"]))
                edit["range"] = (start, end)
                edit["range_fields"] = tuple(field for field in ("in", "out") if field in raw)
            if "position" in raw:
                position = integer(raw["position"], "position", 0, 63)
                require(position < len(document["slices"]), "position_bounds",
                        "Slice position is outside the reel", slice=slice_id, slices=len(document["slices"]))
                edit["position"] = position
            if "label" in raw:
                edit["label"] = text(raw["label"], "slice label")
            edits.append(edit)
        return edits

    def _apply_edits(self, document, edits):
        slices = copy.deepcopy(document["slices"])
        for edit in edits:
            index = next(position for position, item in enumerate(slices) if item["id"] == edit["slice"])
            item = slices[index]
            if "range" in edit:
                item["in_tick"], item["out_tick"] = edit["range"]
            if "label" in edit:
                item["label"] = edit["label"]
            if "position" in edit:
                slices.pop(index)
                slices.insert(edit["position"], item)
        cursor = 0
        for item in slices:
            item["at_tick"] = cursor
            cursor += item["out_tick"] - item["in_tick"]
        require(cursor <= self.settings.max_seconds * TICKS_PER_SECOND,
                "reel_duration", "Reel exceeds service duration limit")
        return {"title": document["title"], "slices": slices, "duration_tick": cursor}

    def _reconcile(self, base, head, edits):
        """Classify requested edits against a newer head revision.

        An edit is keepable when its target slice still exists and no field the
        edit touches has been changed by the other editor.  Ambiguous edits are
        reported for reconfirmation; nothing is written from a stale If-Match.
        """
        base_positions = {item["id"]: index for index, item in enumerate(base["slices"])}
        head_positions = {item["id"]: index for index, item in enumerate(head["slices"])}
        kept, reconfirm = [], []
        for edit in edits:
            slice_id = edit["slice"]
            base_item = base["slices"][base_positions[slice_id]]
            reasons = []
            if slice_id not in head_positions:
                reasons.append("removed")
            else:
                head_item = head["slices"][head_positions[slice_id]]
                if "range" in edit:
                    if base_item["asset"] != head_item["asset"]:
                        reasons.append("source_asset")
                    # Only the endpoints the edit actually changes must be untouched.
                    if "in" in edit["range_fields"] and base_item["in_tick"] != head_item["in_tick"]:
                        reasons.append("source_in")
                    if "out" in edit["range_fields"] and base_item["out_tick"] != head_item["out_tick"]:
                        reasons.append("source_out")
                if "label" in edit and base_item["label"] != head_item["label"]:
                    reasons.append("label")
                if "position" in edit and base_positions[slice_id] != head_positions[slice_id]:
                    reasons.append("order")
            if reasons:
                entry = {"slice": slice_id, "reason": reasons[0], "reasons": reasons,
                         "requested": self._edit_request(edit),
                         "base": self._slice_view(base_item, base_positions[slice_id])}
                if slice_id in head_positions:
                    entry["current"] = self._slice_view(head["slices"][head_positions[slice_id]],
                                                        head_positions[slice_id])
                else:
                    entry["current"] = None
                reconfirm.append(entry)
            else:
                kept.append(edit)
        return kept, reconfirm

    def _edit_request(self, edit):
        request = {"slice": edit["slice"]}
        if "range" in edit:
            if "in" in edit["range_fields"]:
                request["in"] = round(seconds(edit["range"][0]), 6)
            if "out" in edit["range_fields"]:
                request["out"] = round(seconds(edit["range"][1]), 6)
        if "position" in edit:
            request["position"] = edit["position"]
        if "label" in edit:
            request["label"] = edit["label"]
        return request

    def _slice_view(self, item, position):
        return {"position": position, "in": round(seconds(item["in_tick"]), 6),
                "out": round(seconds(item["out_tick"]), 6), "at": round(seconds(item["at_tick"]), 6),
                "label": item["label"]}

    def get(self, reel_id, revision=None, db=None):
        if db is None:
            with self.db.read() as read:
                return self.get(reel_id, revision, read)
        row = db.execute("SELECT * FROM reels WHERE id=?", (reel_id,)).fetchone()
        if row is None:
            raise missing("reel", reel_id)
        target = row["head"] if revision is None else integer(revision, "revision", 1)
        rev = db.execute("SELECT * FROM revisions WHERE reel=? AND number=?", (reel_id, target)).fetchone()
        if rev is None:
            raise missing("revision", target)
        return {**dict(row), "revision": target, "document": json.loads(rev["document"]), "hash": rev["hash"]}

    def list(self):
        with self.db.read() as db:
            return [self.get(row["id"], db=db) for row in db.execute("SELECT id FROM reels ORDER BY created DESC LIMIT 100")]

    def seal(self, reel_id, expected):
        with self.db.write_unit() as db:
            reel = self.get(reel_id, db=db)
            if reel["head"] != expected:
                raise revision_conflict(expected, reel["head"])
            db.execute("UPDATE reels SET state='sealed' WHERE id=?", (reel_id,))
            self.db.event(db, reel_id, "sealed", {"revision": expected})
            return self.get(reel_id, db=db)

    def history(self, reel_id):
        with self.db.read() as db:
            self.get(reel_id, db=db)
            rows = db.execute("SELECT number,hash,created,document FROM revisions WHERE reel=? ORDER BY number DESC LIMIT 100", (reel_id,))
            result = []
            for row in rows:
                document = json.loads(row["document"])
                result.append({"revision": row["number"], "hash": row["hash"], "created": row["created"],
                               "title": document["title"], "slices": len(document["slices"]),
                               "duration_tick": document["duration_tick"]})
            return result
