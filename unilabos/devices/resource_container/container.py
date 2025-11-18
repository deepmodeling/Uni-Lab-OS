
class HotelContainer:
    def __init__(self, rotation: dict, device_config: dict ,**kwargs):
        self.rotation = rotation
        self.device_config = device_config
        self.status = 'idle'

    def get_rotation(self):
        return self.rotation
    

class DeckContainer:
    def __init__(self, rotation: dict, **kwargs):
        self.rotation = rotation
        self.status = 'idle'

    def get_rotation(self):
        return self.rotation
    
class TipRackContainer:
    def __init__(self, rotation: dict, **kwargs):
        self.rotation = rotation
        self.status = 'idle'

    def get_rotation(self):
        return self.rotation
    
class PlateContainer:
    def __init__(self, rotation: dict, **kwargs):
        self.rotation = rotation
        self.status = 'idle'

    def get_rotation(self):
        return self.rotation

class TubeRackContainer:
    def __init__(self, rotation: dict, **kwargs):
        self.rotation = rotation
        self.status = 'idle'

    def get_rotation(self):
        return self.rotation

class BottleRackContainer:
    def __init__(self, rotation: dict, **kwargs):
        self.rotation = rotation
        self.status = 'idle'

    def get_rotation(self):
        return self.rotation