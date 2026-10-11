"""Choosing whom to track when several people are in the frame."""
import numpy as np

import people


def person(cx, cy=0.5, vis=1.0):
    """Landmarks for someone whose torso is centred at (cx, cy): shoulders, hips and the rest nearby."""
    img = np.full((33, 3), 0.0)
    img[:, 0], img[:, 1] = cx, cy
    img[11], img[12] = (cx + 0.05, cy - 0.1, 0), (cx - 0.05, cy - 0.1, 0)
    img[23], img[24] = (cx + 0.04, cy + 0.1, 0), (cx - 0.04, cy + 0.1, 0)
    return img, np.full(33, vis)


def test_the_torso_centre_ignores_hidden_landmarks():
    img, vis = person(0.3, 0.4)
    img[0] = (0.9, 0.9, 0)                      # a face point far away
    assert np.allclose(people.torso_center(img, vis), (0.3, 0.4))
    vis[[23, 24]] = 0.0                          # hips out of frame: shoulders only
    assert np.allclose(people.torso_center(img, vis), (0.3, 0.3))
    vis[:] = 0.0                                 # nothing visible: still an answer, not a crash
    assert people.torso_center(img, vis) is not None


def test_the_person_nearest_the_centre_is_chosen():
    s = people.Selector()
    assert s.choose([(0.15, 0.5), (0.55, 0.5), (0.9, 0.5)]) == 1
    assert s.people == 3 and not s.switched
    assert people.Selector().choose([(0.5, 0.1), (0.2, 0.5)]) == 1      # vertical distance counts too
    assert people.Selector().choose([]) is None


def test_pick_works_from_mediapipe_style_poses():
    left, right = person(0.2), person(0.6)
    s = people.Selector()
    assert s.pick([left[0], right[0]], [left[1], right[1]]) == 1
    assert s.pick([], []) is None and s.people == 0


def test_two_people_at_a_similar_distance_do_not_make_the_tracking_flicker():
    s = people.Selector()
    assert s.choose([(0.40, 0.5), (0.65, 0.5)]) == 0                    # 0.10 vs 0.15 from the centre
    for _ in range(20):                                                 # they sway, the other is now a hair closer
        assert s.choose([(0.43, 0.5), (0.57, 0.5)]) == 0 and not s.switched
        assert s.choose([(0.44, 0.5), (0.56, 0.5)]) == 0


def test_someone_clearly_closer_takes_over_and_it_is_reported():
    s = people.Selector()
    assert s.choose([(0.30, 0.5), (0.8, 0.5)]) == 0
    assert s.choose([(0.30, 0.5), (0.52, 0.5)]) == 1 and s.switched     # the other stepped to the middle
    assert s.choose([(0.30, 0.5), (0.52, 0.5)]) == 1 and not s.switched


def test_the_person_leaving_hands_over_to_whoever_is_left():
    s = people.Selector()
    assert s.choose([(0.5, 0.5), (0.9, 0.5)]) == 0
    assert s.choose([(0.9, 0.5)]) == 0 and s.switched                   # the patient left: someone new


def test_a_few_missed_frames_do_not_count_as_a_new_person():
    s = people.Selector()
    s.choose([(0.45, 0.5)])
    for _ in range(5):
        assert s.choose([]) is None
    assert s.choose([(0.46, 0.5)]) == 0 and not s.switched              # same person, brief dropout
    for _ in range(people.FORGET_AFTER):
        s.choose([])
    assert s.choose([(0.46, 0.5)]) == 0 and not s.switched              # long gone: nothing to compare with
