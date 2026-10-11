"""Baymax (Hiwonder TonyPi Pro) robot API.

Layers:
  servo/    most granular: individual bus servos, head
  actions/  preprogrammed moves: walk, turn, side step, arms, grippers, poses
  sensors/  ultrasonic, climate, light, dot matrix, voice box, microphone,
            buzzer, battery/servo health

api.py registers every operation by path; server.py serves them over HTTP,
client.py calls that server, tools.py wraps either backend as LangChain tools,
agent.py runs a Claude agent on top. See robot_api/README.md.
"""
