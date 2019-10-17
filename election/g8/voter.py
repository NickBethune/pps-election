from collections import defaultdict
import math
import numpy as np
import copy
import json
from sklearn.cluster import MiniBatchKMeans
from sklearn.neighbors import KDTree
from shapely.geometry import Point, Polygon, LineString
from scipy.spatial import Voronoi


WINNER_TAKE_ALL = False
# From which parties perspective are we gerrymandering
GERRY_FOR_P1 = False
# Use seats won as evolutionary criteria / alternatively the mean of efficiency gap and asymmmetry AUC
GERRY_OBJECTIVE_SEATS_WON = False

NUM_VOTERS = 33333
RANDOM_SEED = 1992
NUM_REBALANCE = 100
LOAD_CLUSTERS = True
POPULATION_BOUNDS = 0.1
TARGET_INIT_POPULATION_BOUNDS = 0.1

np.random.seed(RANDOM_SEED)


def BalancedClustering(k, cluster_centroids, points):
    X = points.copy()
    cluster_centers = cluster_centroids.copy()

    n = math.ceil(X.shape[0]/k*1.05)

    centroids = np.zeros((k,2))
    cardinality = np.array([0] * k)

    for i in range(X.shape[0]):
        tree = KDTree(cluster_centers)
        nearest_dist, nearest_ind = tree.query([X[i,:]], k=1)
        p = cluster_centers[nearest_ind[0]]
        for j in range(cluster_centroids.shape[0]):
            if p[0,0] == cluster_centroids[j,0] and p[0,1] == cluster_centroids[j,1]:
                idx = j
        cardinality[idx] += 1
        centroids[idx,:] += X[i,:]
        if(cardinality[idx] > n):
            cluster_centers = np.delete(cluster_centers, nearest_ind[0], axis=0)

    for i in range(centroids.shape[0]):
        centroids[i,:] /= cardinality[i]

    return centroids


class Voter:
    def __init__(self, x, y, prefs):
        self.x = x
        self.y = y
        self.prefs = prefs


def extractVoters(filename):
    f = open(filename, "r")
    content = f.readlines()
    voters = list()
    for i in range(1, len(content)-1):
        line = content[i].split()
        x = float(line[0])
        y = float(line[1])
        prefs = list(map(float, line[2:]))
        voters.append(Voter(x, y, prefs))

    return voters


def out_of_bounds(x, y):
    center = (500, 250 * np.sqrt(3))
    top = (500, 500 * math.sqrt(3))
    left = (0, 0)
    right = (1000, 0)
    line = LineString([(x, y), center])
    e1 = LineString([left, right])
    e2 = LineString([left, top])
    e3 = LineString([right, top])
    return line.intersects(e1) or line.intersects(e2) or line.intersects(e3)


def sample_new_point(prev_x, prev_y, area):
    mean = [0, 0]
    cov = [[area, 0], [0, area]]
    ct = 0
    while True:
        delta_x, delta_y = np.random.multivariate_normal(mean, cov, 1).T
        delta_x = delta_x[0]
        delta_y = delta_y[0]
        new_x, new_y = prev_x + delta_x, prev_y + delta_y
        if not out_of_bounds(new_x, new_y):
            return new_x, new_y
        ct += 1
        if ct > 10:
            # TODO fix.  Some points are out of bounds before sampling
            return new_x, new_y


def asymmetry_score(districts, voters, voters_by_district):
    seats_by_vote_perc = {}
    total_wasted_votes = np.zeros([2, ])
    variations = np.arange(0.3, 0.8, .1)

    # Baseline performance
    _, baseline_seats, _ = get_result(districts, voters, voters_by_district)
    baseline_seats = baseline_seats[0] if GERRY_FOR_P1 else baseline_seats[1]

    for target_v in variations:
        new_voters = copy.deepcopy(voters)
        for v in new_voters:
            v.prefs = adjust_voter_preference(v.prefs, target_p2=target_v)
        popular_vote, seats, wasted_votes = get_result(districts, new_voters, voters_by_district)
        p2_vote_perc = popular_vote[1] / float(len(voters))
        seats_by_vote_perc[p2_vote_perc] = seats[1] / 243.0
        total_wasted_votes += np.array(wasted_votes)
    avg_wasted_votes = total_wasted_votes / float(len(variations))
    avg_efficiency_gap = (avg_wasted_votes[0] - avg_wasted_votes[1]) / float(len(voters))
    avg_pref_variation = np.mean(np.array(list(seats_by_vote_perc.keys())))
    assert avg_pref_variation > 0.45 and avg_pref_variation < 0.55
    avg_votes_to_seats = np.mean(np.array(list(seats_by_vote_perc.values())))
    avg_votes_to_seats_norm = 2 * avg_votes_to_seats - 1
    return (avg_votes_to_seats_norm + avg_efficiency_gap) / 2.0, baseline_seats, seats_by_vote_perc


def find_voter_district(districts, voter, recent_district_idxs=[]):
    p = Point(voter.x, voter.y)
    for idx in recent_district_idxs:
        if districts[idx].contains(p):
            return idx
    for idx, district in enumerate(districts):
        if district.contains(p):
            return idx


def compute_seat_count(party_votes):
    N = float(sum(party_votes))
    p1_pref = party_votes[0] / N
    p2_pref = party_votes[1] / N
    if WINNER_TAKE_ALL:
        if p1_pref > p2_pref:
            return [1, 0]
        else:
            return [0, 1]
    p1_seats, p2_seats = 0, 0
    while p1_seats + p2_seats < 3:
        if p1_pref > p2_pref:
            p1_seats += 1
            p1_pref -= .25
        else:
            p2_seats += 1
            p2_pref -= .25
    assert p1_seats + p2_seats == 3
    return (p1_seats, p1_pref * N), (p2_seats, p2_pref * N)


def compute_seats(district_votes):
    seats = np.zeros([2, ])
    wasted_votes = np.zeros([2, ])
    for dv in district_votes:
        (p1_seats, p1_pref), (p2_seats, p2_pref) = compute_seat_count(dv)
        seats[0] += p1_seats
        seats[1] += p2_seats
        wasted_votes[0] += p1_pref
        wasted_votes[1] += p2_pref
    return seats, wasted_votes


def get_result(districts, voters, voters_by_district):
    district_votes = np.zeros([len(districts), 2])
    for didx in voters_by_district:
        voter_idxs = voters_by_district[didx]
        for vidx in voter_idxs:
            vote = sample_vote(voters[vidx].prefs)
            district_votes[didx, vote] += 1
    popular_vote = district_votes.sum(0)
    seats, wasted_votes = compute_seats(district_votes)
    return popular_vote, seats, wasted_votes


def adjust_voter_preference(pref, target_p2=0.5):
    p1_boost = 0.5 - target_p2
    p2_boost = target_p2 - 0.5
    beta = max(0.01, p1_boost + pref[0])
    alpha = max(0.01, p2_boost + pref[1])
    p2_prob = np.random.beta(alpha, beta)
    return [1.0 - p2_prob, p2_prob]


def sample_vote(pref):
    if sum(pref) == 1.0:
        return np.random.binomial(size=1, n=1, p=pref[1])[0]
    else:
        p1_pref = pref[0] / float(sum(pref))
        p1_vote = np.random.random() < p1_pref
        return 0 if p1_vote else 1


def is_valid_draw(new_districts, voters, is_gerry=True):
    district_voters = np.zeros([len(districts)])
    voters_by_district = defaultdict(list)
    last_districts = []
    N = float(len(voters))
    mean = N / float(len(new_districts))
    if is_gerry:
        lower = mean * (1.0 - POPULATION_BOUNDS)
        upper = mean * (1.0 + POPULATION_BOUNDS)
    else:
        lower = mean * (1.0 - TARGET_INIT_POPULATION_BOUNDS)
        upper = mean * (1.0 + TARGET_INIT_POPULATION_BOUNDS)
    for vidx, voter in enumerate(voters):
        district_idx = find_voter_district(new_districts, voter, last_districts)
        voters_by_district[district_idx].append(vidx)
        if district_idx not in last_districts:
            last_districts.append(district_idx)
            if len(last_districts) > 3:
                last_districts = last_districts[1:]
        district_voters[district_idx] += 1

    district_voters = np.array(district_voters)
    sorted_pop_idxs = np.argsort(district_voters)
    district_voters_sorted = district_voters[sorted_pop_idxs]

    too_small_breakpoint = 999
    too_big_breakpoint = 999
    for didx, district_votes in enumerate(district_voters_sorted):
        if district_votes > lower:
            too_small_breakpoint = min(too_small_breakpoint, didx)
        if district_votes > upper:
            too_big_breakpoint = min(too_big_breakpoint, didx)

    too_big_district_idxs = []
    too_small_district_idxs = []
    if too_small_breakpoint < 999:
        too_small_district_idxs = sorted_pop_idxs[:too_small_breakpoint]
    if too_big_breakpoint < 999:
        too_big_district_idxs = sorted_pop_idxs[too_big_breakpoint:]

    if len(too_small_district_idxs) + len(too_big_district_idxs) == 0:
        if not is_gerry:
            print('Total Underflow / Overflow is {} / {} voters'.format(0, 0))
        return True, voters_by_district, 0

    underflow = lower - district_voters[too_small_district_idxs]
    overflow = district_voters[too_big_district_idxs] - upper

    total_overflow = overflow.sum()
    total_underflow = underflow.sum()
    if not is_gerry:
        print('Total Underflow / Overflow is {} / {} voters'.format(total_underflow, total_overflow))
    return False, None, total_overflow + total_underflow


def find_closest(centroids, idx, n=2):
    distances = []
    for cidx, centroid in enumerate(centroids):
        if cidx == idx:
            distance = 999999999
        else:
            distance = np.sqrt(np.power(centroid[0] - centroids[idx][0], 2) +
                               np.power(centroid[1] - centroids[idx][1], 2))

        distances.append(distance)

    return np.argsort(np.array(distances))[:n]


def validate(centroids, districts, voters, is_gerry=True):
    new_centroids = centroids
    new_districts = districts
    is_valid, voters_by_district, prev_total_overflow = is_valid_draw(new_districts, voters, is_gerry=is_gerry)
    iteration = 0

    didx = np.random.choice(np.arange(len(centroids)))
    denom = np.log(districts[didx].area) if is_gerry else 5.0

    if is_gerry:
        is_valid = False

    while not is_valid:
        centroid_candidates = new_centroids.copy()
        if not is_gerry:
            didx = np.random.choice(np.arange(len(centroid_candidates)))
        centroid_candidates[didx][0] = centroid_candidates[didx][0] + np.random.normal(0, denom)
        centroid_candidates[didx][1] = centroid_candidates[didx][1] + np.random.normal(0, denom)

        district_candidates = draw_districts(centroid_candidates)
        is_valid, voters_by_district, total_flow = is_valid_draw(district_candidates, voters, is_gerry=is_gerry)
        if total_flow <= prev_total_overflow:
            prev_total_overflow = total_flow
            new_districts = district_candidates
            new_centroids = centroid_candidates
        else:
            if not is_gerry:
                print('Tried unsuccessfully')

        if total_flow < 25.0 and iteration > 1000 and not is_gerry:
            print('Saving almost data!')
            json.dump(new_centroids.tolist(), open('adjusted_data/almost_centroids.json', 'w'))
            np.save(open('adjusted_data/almost_districts.npy', 'wb'), new_districts)
        iteration += 1
    return new_centroids, new_districts, voters_by_district


# Clip the Voronoi Diagram
# Run "conda install shapely -c conda-forge" on terminal first
# Method from StackOverflow
# Reference : https://stackoverflow.com/questions/36063533/clipping-a-voronoi-diagram-python
def voronoi_finite_polygons_2d(vor, radius=None):
    """
    Reconstruct infinite voronoi regions in a 2D diagram to finite
    regions.
    Parameters
    ----------
    vor : Voronoi
        Input diagram
    radius : float, optional
        Distance to 'points at infinity'.
    Returns
    -------
    regions : list of tuples
        Indices of vertices in each revised Voronoi regions.
    vertices : list of tuples
        Coordinates for revised Voronoi vertices. Same as coordinates
        of input vertices, with 'points at infinity' appended to the
        end.
    """

    if vor.points.shape[1] != 2:
        raise ValueError("Requires 2D input")

    new_regions = []
    new_vertices = vor.vertices.tolist()

    center = vor.points.mean(axis=0)
    if radius is None:
        radius = vor.points.ptp().max()*2

    # Construct a map containing all ridges for a given point
    all_ridges = {}
    for (p1, p2), (v1, v2) in zip(vor.ridge_points, vor.ridge_vertices):
        all_ridges.setdefault(p1, []).append((p2, v1, v2))
        all_ridges.setdefault(p2, []).append((p1, v1, v2))

    # Reconstruct infinite regions
    for p1, region in enumerate(vor.point_region):
        vertices = vor.regions[region]

        if all(v >= 0 for v in vertices):
            # finite region
            new_regions.append(vertices)
            continue

        # reconstruct a non-finite region
        ridges = all_ridges[p1]
        new_region = [v for v in vertices if v >= 0]

        for p2, v1, v2 in ridges:
            if v2 < 0:
                v1, v2 = v2, v1
            if v1 >= 0:
                # finite ridge: already in the region
                continue

            # Compute the missing endpoint of an infinite ridge

            t = vor.points[p2] - vor.points[p1] # tangent
            t /= np.linalg.norm(t)
            n = np.array([-t[1], t[0]])  # normal

            midpoint = vor.points[[p1, p2]].mean(axis=0)
            direction = np.sign(np.dot(midpoint - center, n)) * n
            far_point = vor.vertices[v2] + direction * radius

            new_region.append(len(new_vertices))
            new_vertices.append(far_point.tolist())

        # sort region counterclockwise
        vs = np.asarray([new_vertices[v] for v in new_region])
        c = vs.mean(axis=0)
        angles = np.arctan2(vs[:,1] - c[1], vs[:,0] - c[0])
        new_region = np.array(new_region)[np.argsort(angles)]

        # finish
        new_regions.append(new_region.tolist())

    return new_regions, np.asarray(new_vertices)


def draw_districts(centroids):
    vor = Voronoi(centroids)
    # voronoi_plot_2d(vor, show_vertices=False)
    regions, vertices = voronoi_finite_polygons_2d(vor)

    # Box the triangular boundary
    box = Polygon([[0, 0], [1000, 0], [500, 500*math.sqrt(3)]])

    # Final Output Districts
    districts = []
    # Colorize Districts
    for region in regions:
        polygon = vertices[region]
        # Clipping polygon
        poly = Polygon(polygon)
        poly = poly.intersection(box)
        districts.append(poly)
    return districts


if __name__ == '__main__':
    ndist = 243 if WINNER_TAKE_ALL else 81

    if WINNER_TAKE_ALL:
        print('WINNER TAKE ALL!')

    if GERRY_FOR_P1:
        print('Gerrymandering For Party 1!')
    else:
        print('Gerrymandering For Party 2!')

    if GERRY_OBJECTIVE_SEATS_WON:
        print('Optimizing for seats won!')
    else:
        print('Optimizing for mean of efficiency gap and asymmetry AUC!')

    # Extract Voters Positions
    voters = np.array(extractVoters("../../maps/g8/twoParties.map"))
    np.random.shuffle(voters)

    initial_popular_vote = [0, 0]

    for voter in voters:
        prev_prefs = voter.prefs
        initial_popular_vote[0] += prev_prefs[0]
        initial_popular_vote[1] += prev_prefs[1]
        if GERRY_FOR_P1:
            new_prefs = [prev_prefs[1], prev_prefs[0]]
            voter.prefs = new_prefs

    total = float(sum(initial_popular_vote))
    initial_popular_vote = [v / total for v in initial_popular_vote]
    print('Initial preferences: P1={}, P2={}'.format(initial_popular_vote[0], initial_popular_vote[1]))

    voters = voters[:NUM_VOTERS].tolist()
    V = np.vstack([np.array((i.x, i.y)) for i in voters])

    if LOAD_CLUSTERS:
        print('Loading Clusters')
        centroids = np.array(json.load(open('adjusted_data/centroids.json', 'r')))
    else:
        kmeans = MiniBatchKMeans(
            n_clusters=ndist, random_state=0, batch_size=32, max_iter=20, init_size=3 * 81).fit(V)

        centroids = kmeans.cluster_centers_
        for i in range(NUM_REBALANCE):
            print('Rebalancing cluster centers {}/{}'.format(str(i + 1), str(NUM_REBALANCE)))
            centroids = BalancedClustering(ndist, centroids, V)

    # Generate Voronoi with generator points = cluster centroids
    # Note : Some generator points outside triangular boundary due to the error in coordinates.txt data
    districts = draw_districts(centroids)

    # Ensure valid
    centroids, districts, voters_by_district = validate(centroids, districts, voters, is_gerry=False)
    json.dump(centroids.tolist(), open('adjusted_data/centroids.json', 'w'))
    np.save(open('adjusted_data/districts.npy', 'wb'), districts)

    # LOAD INITIAL DISTRICTS
    initial_districts = copy.deepcopy(districts)  # Keep a hardcopy of this

    print('Starting evolutionary approach!')
    party_str = 'party_1' if GERRY_FOR_P1 else 'party_2'

    gerrymander_score, seats_won, seats_by_vote_perc = asymmetry_score(districts, voters, voters_by_district)
    print('Initial Gerrymander Score={}'.format(gerrymander_score))
    print('Initial Seats won={}/{} ({})'.format(int(seats_won), 243, round(seats_won / 243.0, 2)))
    json.dump(seats_by_vote_perc, open('gerrymander_data/initial_asymmetry_curve_for_{}.json'.format(party_str), 'w'))

    best_gerry_score = gerrymander_score
    best_seat_score = seats_won
    for mut_idx in range(1000):
        print('{}/{} Evolutionary iterations complete'.format(mut_idx, 1000))
        all_candidate_districts = []
        all_candidate_centroids = []
        gerrymander_scores = []
        seat_scores = []
        # Randomly jiggle map N times
        N = 3
        for _ in range(N):
            candidate_centroids, candidate_districts, voters_by_district = validate(
                centroids, districts, voters, is_gerry=True)
            all_candidate_districts.append(candidate_districts)
            all_candidate_centroids.append(candidate_centroids)
            gerrymander_score, seats_won, seats_by_vote_perc = asymmetry_score(
                candidate_districts, voters, voters_by_district)

            seat_scores.append(seats_won)
            gerrymander_scores.append(gerrymander_score)

        gerrymander_scores = np.array(gerrymander_scores)
        seat_scores = np.array(seat_scores)
        best_seat_idx = np.argsort(seat_scores)[-1]
        best_gerry_idx = np.argsort(gerrymander_scores)[-1]
        best_seat_score = max(best_seat_score, seat_scores[best_seat_idx])
        best_gerry_score = max(best_gerry_score, gerrymander_scores[best_gerry_idx])

        best_idx = best_seat_idx if GERRY_OBJECTIVE_SEATS_WON else best_gerry_idx
        best_score = best_seat_score if GERRY_OBJECTIVE_SEATS_WON else best_gerry_score

        #  Is the max score the current score
        if GERRY_OBJECTIVE_SEATS_WON:
            has_improved = seat_scores[best_seat_idx] == best_seat_score
        else:
            has_improved = gerrymander_scores[best_gerry_idx] == best_gerry_score

        if has_improved:  # or not made objective worse
            centroids = all_candidate_centroids[best_idx]
            # Choose best district from gerrymandering perspective
            districts = all_candidate_districts[best_idx]
            print('Most seats at {} is {}/{}'.format(mut_idx, int(best_seat_score), 243))
            print('Best Gerrymander Score (-1, 1) at {} is={}'.format(mut_idx, best_gerry_score))
            np.save(open('gerrymander_data/best_districts_for_{}_at_{}.npy'.format(party_str, mut_idx), 'wb'), districts)
            json.dump(centroids.tolist(), open('gerrymander_data/best_centroids_for_{}_at_{}.json'.format(
                party_str, mut_idx), 'w'))
            json.dump(seats_by_vote_perc, open('gerrymander_data/asymmetry_curve_for_{}_at_{}.json'.format(
                party_str, mut_idx), 'w'))
        else:
            print('Didn\'t improve.  Trying again!')

    print('Best gerrymander score (-1, 1) is {}'.format(best_score))
