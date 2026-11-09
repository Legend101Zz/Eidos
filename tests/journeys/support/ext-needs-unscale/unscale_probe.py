class Probe:
    phases = ("after-gradient-unscale",)

    def __init__(self, config, context):
        self.n = 0

    def on_phase(self, phase, **event):
        self.n += 1

    def receipt(self):
        return {"calls": self.n}


def create(config, context):
    return Probe(config, context)
