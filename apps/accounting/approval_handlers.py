"""Approval decisions for accounting documents (see core.approval_services)."""
from apps.accounting import note_services
from apps.core.approval_services import approval_handler, document_uuid


@approval_handler(note_services.NOTE_APPROVAL_TYPE)
def on_standalone_note(req, decision):
    """Approved: the note becomes Approved, ready to post. Rejected or
    returned: back to Draft for correction."""
    from apps.accounting.models import StandaloneNote

    doc_id = document_uuid(req)
    if doc_id is None:
        return
    note = StandaloneNote.objects.filter(client_id=req.client_id, id=doc_id).first()
    if note is None or note.status != "Pending Approval":
        return
    if decision == "Approved":
        note_services.approve_standalone_note(note, user=req.decided_by)
    else:
        note_services.return_standalone_note(note, user=req.decided_by)
