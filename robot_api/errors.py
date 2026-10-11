class RobotError(Exception):
    """A request the robot refused or could not carry out (too hot, bad name,
    sensor missing...). The message is meant for the caller, human or model."""
