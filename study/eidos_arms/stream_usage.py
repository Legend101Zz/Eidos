"""Bridge provider message_delta receipts into lane A's per-message token tracker."""


def extend_tracker(base):
    class PartialUsageTracker(base):
        def __init__(self):
            super().__init__()
            self.active = {}

        def observe(self, event):
            super().observe(event)
            if event.get('type') != 'stream_event':
                return
            part = event.get('event') or {}
            parent = event.get('parent_tool_use_id') or '__main'
            if part.get('type') == 'message_start':
                message = part.get('message') or {}
                self.active[parent] = {'id': message.get('id'), 'model': message.get('model'),
                                       'usage': dict(message.get('usage') or {})}
            elif part.get('type') == 'message_delta' and parent in self.active:
                self.active[parent]['usage'].update(part.get('usage') or {})
            if parent in self.active:
                message = self.active[parent]
                super().observe({'type': 'assistant', 'message': message})
    return PartialUsageTracker
