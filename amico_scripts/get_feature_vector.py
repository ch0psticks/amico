#! /usr/bin/python
###########################################################################
# Copyright (C) 2012 Phani Vadrevu and Roberto Perdisci                   #
# pvadrevu@uga.edu                                                        #
#                                                                         #
# Distributed under the GNU Public License                                #
# http://www.gnu.org/licenses/gpl.txt                                     #   
#                                                                         #
# This program is free software; you can redistribute it and/or modify    #
# it under the terms of the GNU General Public License as published by    #
# the Free Software Foundation; either version 2 of the License, or       #
# (at your option) any later version.                                     #
#                                                                         #
###########################################################################
import time
import urlparse
import re
import psycopg2
import util
import sys
import numpy as np
import pandas as ps
from datetime import timedelta
from config import MAX_PAST_DUMPS, MAX_PAST_DAYS




# TODO: Don't let the hash_life_time and num_dumps_with_same_hash values be null
# TODO: Speed up the script
# TODO: Verify how null values of x_malware_ratio features are handled by WEKA


def insert_host_based_features(cursor, dump_id):
    """ Computes hostname-based features for a given download
    
    Arguments:
        cursor: DB cursort from existing DB connection
        dump_id: id of download to be classified 

    """

    # also query for timestamp, so we can use to limit how much we go back in time!
    query = " SELECT host,DATE(timestamp) FROM pe_dumps WHERE dump_id = %s " 

    cursor.execute(query,(dump_id, ))
    row = cursor.fetchone()
    if not row:
        return

    (host,date) = row

    query = """
        SELECT dump_id,pe.sha1,trusted_av_labels,num_av_labels
        FROM pe_dumps AS pe 
        JOIN ped_vts_mapping AS pvm 
          USING(dump_id)
        JOIN virus_total_scans AS vts
          USING(vt_id)
        WHERE pe.corrupt = 'f' AND
              pe.host = %s AND
              pe.dump_id < %s AND pe.dump_id > %s AND
              pe.timestamp > %s """
    
    cursor.execute(query,(host, dump_id, dump_id-MAX_PAST_DUMPS, date-timedelta(days=MAX_PAST_DAYS)))
    tuples = cursor.fetchall()

    if not tuples:
        return 
    
    # make the results into a pandas data frame
    df = ps.DataFrame.from_records(tuples)
    df.columns = ['dump_id','sha1','tavs','navs']

    tavs_threshold = 1
    navs_threshold = 0
    host_total_downloads = len(set(df['dump_id']))
    host_benign_downloads = len(set(df[df.navs==0]['dump_id']))
    host_malware_downloads = len(set(df[df.tavs>tavs_threshold]['dump_id']))
    host_suspicious_downloads = len(set(df[(df.tavs<=tavs_threshold) & (df.navs>navs_threshold)]['dump_id']))
    host_total_hashes = len(set(df['sha1']))
    host_unknown_hashes = len(set(df[df.navs.isnull()]['sha1']))

    host_benign_ratio = float(host_benign_downloads)/host_total_downloads
    host_malware_ratio = float(host_malware_downloads)/host_total_downloads
    host_suspicious_ratio = float(host_suspicious_downloads)/host_total_hashes
    host_unknown_hash_ratio = float(host_unknown_hashes)/host_total_hashes

    host_avg_av_labels = None
    host_avg_trusted_labels = None

    sha1_nav_labels = []
    sha1_tav_labels = []
    for (sha,dfgroup) in df.groupby('sha1'):
        d = dfgroup.sort_values('dump_id',ascending=False)
        if d['navs'].iat[0] != None and not np.isnan(d['navs'].iat[0]):
            sha1_nav_labels.append(d['navs'].iat[0])
            sha1_tav_labels.append(d['tavs'].iat[0])
    if(len(sha1_nav_labels) > 0):
        host_avg_av_labels = np.mean(sha1_nav_labels)
        host_avg_trusted_labels = np.mean(sha1_tav_labels)


    query = """
                UPDATE weka_features set host_benign_downloads = %s,
                 host_malware_downloads = %s,
                 host_suspicious_downloads = %s,
                 host_total_downloads = %s,
                 host_malware_ratio = %s,
                 host_suspicious_ratio = %s,
                 host_benign_ratio = %s,
                 host_avg_av_labels = %s,
                 host_avg_trusted_labels = %s,
                 host_unknown_hashes = %s,
                 host_total_hashes = %s,
                 host_unknown_hash_ratio = %s
                 where dump_id = %s """

    try:
        cursor.execute(query,(host_benign_downloads, host_malware_downloads,
                 host_suspicious_downloads,
                 host_total_downloads, host_malware_ratio,
                 host_suspicious_ratio,
                 host_benign_ratio,
                 host_avg_av_labels, host_avg_trusted_labels,
                 host_unknown_hashes, host_total_hashes,
                 host_unknown_hash_ratio, dump_id ))
    except Exception as e:
        print e
        print "Could not insert host based features for the dump #", dump_id    
    
        
def insert_twold_based_features(cursor, dump_id):
    cursor.execute("""
           SELECT host FROM pe_dumps where
           dump_id = %s""", (dump_id, ))
    row = cursor.fetchone()
    try:
        # ok because AND clauses are evaluated left to right
        if row is not None and row[0]:
            host = util.reorder_domain(row[0])
            twold = util.extract_twold(host)
            twold = util.reorder_domain(twold)
            twold += '%'
        else:
            print "host is None!"
            return
    except Exception as e:
        # capturing known causes
        if util.is_ip(host):
            twold = row[0]
        else:
            print "Error in extracting 2LD!, ", e, host, dump_id
            return

    cursor.execute("""
        SELECT COUNT(DISTINCT dump_id)
        FROM pe_dumps AS pe
        WHERE pe.host LIKE %s AND
            pe.corrupt = 'f' AND
            pe.dump_id < %s AND pe.dump_id > %s""",
        (twold, dump_id, dump_id-MAX_PAST_DUMPS))
    twold_total_downloads = cursor.fetchone()[0]

    # Disabled vt_month_shelf due to the 403 error from VT
    #cursor.execute("""
    #    SELECT count(distinct dump_id) from pe_dumps as pe JOIN 
    #    weka_features as f using (dump_id)
    #    where f.raw_dump_num_av_labels = 0 and f.vt_month_shelf = 't' and 
    #    pe.host like %s and pe.dump_id < %s """,
    #    (twold, dump_id))
    cursor.execute("""
        SELECT COUNT(DISTINCT dump_id)
        FROM pe_dumps AS pe JOIN
            ped_vts_mapping AS pvm USING (dump_id),
            virus_total_scans AS vts
        WHERE vts.num_av_labels = 0 AND
            pe.corrupt = 'f' AND
            pe.host LIKE %s AND
            pe.dump_id < %s AND pe.dump_id > %s AND
            vts.vt_id = pvm.vt_id""",
        (twold, dump_id, dump_id-MAX_PAST_DUMPS))
    twold_benign_downloads = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(DISTINCT dump_id)
        FROM pe_dumps AS pe JOIN
            ped_vts_mapping AS pvm USING (dump_id),
            virus_total_scans AS vts
        WHERE vts.trusted_av_labels > 1 AND
            pe.corrupt = 'f' AND
            pe.host LIKE %s AND
            pe.dump_id < %s AND pe.dump_id > %s AND
            vts.vt_id = pvm.vt_id""",
        (twold, dump_id, dump_id-MAX_PAST_DUMPS))
    twold_malware_downloads = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(DISTINCT dump_id)
        FROM pe_dumps AS pe JOIN
            ped_vts_mapping AS pvm USING (dump_id),
            virus_total_scans AS vts
        WHERE vts.num_av_labels > 1 AND
            pe.corrupt = 'f' AND
            pe.host LIKE %s AND
            pe.dump_id < %s AND pe.dump_id > %s AND
            vts.vt_id = pvm.vt_id""",
        (twold, dump_id, dump_id-MAX_PAST_DUMPS))
    twold_suspicious_downloads = cursor.fetchone()[0]

    if twold_total_downloads == 0:
        twold_benign_ratio = None
        twold_malware_ratio = None
        twold_suspicious_ratio = None
    else:
        twold_benign_ratio = float(twold_benign_downloads) / twold_total_downloads
        twold_malware_ratio = float(twold_malware_downloads) / twold_total_downloads
        twold_suspicious_ratio = float(twold_suspicious_downloads) / twold_total_downloads

    # The averages are over distinct sha1s
    cursor.execute("""
        SELECT AVG(num_av_labels), AVG(trusted_av_labels)
        FROM
            (SELECT pe.sha1, MAX(dump_id) AS max_id
            FROM pe_dumps AS pe
            WHERE pe.host LIKE %s AND
                pe.dump_id < %s AND pe.dump_id > %s AND
                pe.corrupt = 'f' GROUP BY pe.sha1) as a
            JOIN
            (SELECT p.sha1, num_av_labels, trusted_av_labels, dump_id
            FROM pe_dumps AS p JOIN
                ped_vts_mapping as pvm USING (dump_id),
                virus_total_scans as vts
            WHERE pvm.vt_id = vts.vt_id AND
                p.host LIKE %s AND
                dump_id < %s AND dump_id > %s AND
                p.corrupt='f') as b
            ON a.max_id = b.dump_id
        WHERE num_av_labels IS NOT NULL""",
    (twold, dump_id, dump_id-MAX_PAST_DUMPS, twold, dump_id, dump_id-MAX_PAST_DUMPS))
    if cursor.rowcount == 0:
        twold_avg_av_labels = None
        twold_avg_trusted_labels = None
    else:
        twold_avg_av_labels, twold_avg_trusted_labels = cursor.fetchone()


    # the oldest scan report is used to get the # of unknown hashes
    # to remove any bias due to VT submissions
    cursor.execute("""
        SELECT COUNT(DISTINCT b.sha1)
        FROM
            (SELECT pe.sha1, MIN(dump_id) AS min_id
            FROM pe_dumps AS pe
            WHERE pe.host LIKE %s AND
                pe.dump_id < %s AND pe.dump_id > %s AND
                pe.corrupt = 'f' GROUP BY pe.sha1) as a
            JOIN
            (SELECT p.sha1, num_av_labels, trusted_av_labels, dump_id
            FROM pe_dumps AS p JOIN
                ped_vts_mapping as pvm USING (dump_id),
                virus_total_scans as vts
            WHERE pvm.vt_id = vts.vt_id AND
                p.host LIKE %s AND
                dump_id < %s AND dump_id > %s AND
                p.corrupt='f') as b
            ON a.min_id = b.dump_id
        WHERE num_av_labels IS NULL""",
        (twold, dump_id, dump_id-MAX_PAST_DUMPS, twold, dump_id, dump_id-MAX_PAST_DUMPS))
    twold_unknown_hashes = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(DISTINCT pe.sha1)
        FROM pe_dumps AS pe
        WHERE pe.host LIKE %s AND
            pe.corrupt = 'f' AND
            pe.dump_id < %s AND pe.dump_id > %s """,
        (twold, dump_id, dump_id-MAX_PAST_DUMPS))
    twold_total_hashes = cursor.fetchone()[0]
    if twold_total_hashes != 0:
        twold_unknown_hash_ratio = float(twold_unknown_hashes) / twold_total_hashes
    else:
        twold_unknown_hash_ratio = None

    try:
        cursor.execute("""
                UPDATE weka_features set twold_benign_downloads = %s,
                 twold_malware_downloads = %s,
                 twold_suspicious_downloads = %s,
                 twold_total_downloads = %s,
                 twold_malware_ratio = %s,
                 twold_suspicious_ratio = %s,
                 twold_benign_ratio = %s,
                 twold_avg_av_labels = %s,
                 twold_avg_trusted_labels = %s,
                 twold_unknown_hashes = %s,
                 twold_total_hashes = %s,
                 twold_unknown_hash_ratio = %s
                 where dump_id = %s """,
                (twold_benign_downloads, twold_malware_downloads, 
                 twold_suspicious_downloads,
                 twold_total_downloads, twold_malware_ratio,
                 twold_suspicious_ratio,
                 twold_benign_ratio, 
                 twold_avg_av_labels, twold_avg_trusted_labels,
                 twold_unknown_hashes, twold_total_hashes, 
                 twold_unknown_hash_ratio, dump_id))
    except Exception as e:
        print e
        print "Could not insert twold based features for the dump #", dump_id

def insert_server_ip_based_features(cursor, dump_id):
    cursor.execute("""
            SELECT server from pe_dumps where dump_id = %s""", (dump_id, ))
    row = cursor.fetchone()
    if row is not None:
        server_ip = row[0]
    else:
        return

    cursor.execute("""
        SELECT COUNT(DISTINCT dump_id)
        FROM pe_dumps AS pe
        WHERE pe.server = %s AND
            pe.corrupt = 'f' AND
            pe.dump_id < %s AND pe.dump_id > %s """,
        (server_ip, dump_id, dump_id-MAX_PAST_DUMPS))
    server_ip_total_downloads = cursor.fetchone()[0]
    #print "server_ip_total_downloads:", server_ip_total_downloads

    # Disabled vt_month_shelf due to the 403 error from VT
    #cursor.execute("""
    #    SELECT count(distinct dump_id) from pe_dumps as pe JOIN 
    #    weka_features as f using (dump_id)
    #    where f.raw_dump_num_av_labels = 0 and f.vt_month_shelf = 't' and  
    #    pe.server = %s and pe.dump_id < %s """,
    #    (server_ip, dump_id))
    cursor.execute("""
        SELECT COUNT(DISTINCT dump_id)
        FROM pe_dumps AS pe JOIN
            ped_vts_mapping AS pvm USING (dump_id),
            virus_total_scans AS vts
        WHERE vts.num_av_labels = 0 AND
            pe.corrupt = 'f' AND
            pe.server = %s AND
            pe.dump_id < %s AND pe.dump_id > %s AND
            vts.vt_id = pvm.vt_id""",
        (server_ip, dump_id, dump_id-MAX_PAST_DUMPS))
    server_ip_benign_downloads = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(DISTINCT dump_id)
        FROM pe_dumps AS pe JOIN
            ped_vts_mapping AS pvm USING (dump_id),
            virus_total_scans AS vts
        WHERE vts.trusted_av_labels > 1 AND
            pe.corrupt = 'f' AND
            pe.server = %s AND
            pe.dump_id < %s AND pe.dump_id >  %s AND
            vts.vt_id = pvm.vt_id""",
        (server_ip, dump_id, dump_id-MAX_PAST_DUMPS))
    server_ip_malware_downloads = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(DISTINCT dump_id)
        FROM pe_dumps AS pe JOIN
            ped_vts_mapping AS pvm USING (dump_id),
            virus_total_scans AS vts
        WHERE vts.num_av_labels > 1 AND
            pe.corrupt = 'f' AND
            pe.server = %s AND
            pe.dump_id < %s AND pe.dump_id > %s AND
            vts.vt_id = pvm.vt_id""",
        (server_ip, dump_id, dump_id-MAX_PAST_DUMPS))
    server_ip_suspicious_downloads = cursor.fetchone()[0]

    if server_ip_total_downloads == 0:
        server_ip_benign_ratio = None
        server_ip_malware_ratio = None
        server_ip_suspicious_ratio = None
    else:
        server_ip_benign_ratio = float(server_ip_benign_downloads) / server_ip_total_downloads
        server_ip_malware_ratio = float(server_ip_malware_downloads) / server_ip_total_downloads
        server_ip_suspicious_ratio = float(server_ip_suspicious_downloads) / server_ip_total_downloads

    # The averages are over distinct sha1s
    cursor.execute("""
        SELECT AVG(num_av_labels), AVG(trusted_av_labels)
        FROM
            (SELECT pe.sha1, MAX(dump_id) AS max_id
            FROM pe_dumps AS pe
            WHERE pe.server = %s AND
                pe.dump_id < %s AND pe.dump_id > %s AND
                pe.corrupt = 'f' GROUP BY pe.sha1) as a
            JOIN
            (SELECT p.sha1, num_av_labels, trusted_av_labels, dump_id
            FROM pe_dumps AS p JOIN
                ped_vts_mapping as pvm USING (dump_id),
                virus_total_scans as vts
            WHERE pvm.vt_id = vts.vt_id AND
                p.server = %s AND
                dump_id < %s AND dump_id > %s AND
                p.corrupt='f') as b
            ON a.max_id = b.dump_id
        WHERE num_av_labels IS NOT NULL""",
    (server_ip, dump_id, dump_id-MAX_PAST_DUMPS, server_ip, dump_id, dump_id-MAX_PAST_DUMPS))
    if cursor.rowcount == 0:
        server_ip_avg_av_labels = None
        server_ip_avg_trusted_labels = None
    else:
        server_ip_avg_av_labels, server_ip_avg_trusted_labels = cursor.fetchone()

    # the oldest scan report is used to get the # of unknown hashes
    # to remove any bias due to VT submissions
    cursor.execute("""
        SELECT COUNT(DISTINCT b.sha1)
        FROM
            (SELECT pe.sha1, MIN(dump_id) AS min_id
            FROM pe_dumps AS pe
            WHERE pe.server = %s AND
                pe.dump_id < %s AND pe.dump_id > %s AND
                pe.corrupt = 'f' GROUP BY pe.sha1) as a
            JOIN
            (SELECT p.sha1, num_av_labels, trusted_av_labels, dump_id
            FROM pe_dumps AS p JOIN
                ped_vts_mapping as pvm USING (dump_id),
                virus_total_scans as vts
            WHERE pvm.vt_id = vts.vt_id AND
                p.server = %s AND
                dump_id < %s AND dump_id > %s AND
                p.corrupt='f') as b
            ON a.min_id = b.dump_id
        WHERE num_av_labels IS NULL""",
    (server_ip, dump_id, dump_id-MAX_PAST_DUMPS, server_ip, dump_id, dump_id-MAX_PAST_DUMPS))
    server_ip_unknown_hashes = cursor.fetchone()[0]
    
    cursor.execute("""
        SELECT COUNT(DISTINCT pe.sha1)
        FROM pe_dumps AS pe
        WHERE pe.server = %s AND
            pe.corrupt = 'f' AND
            pe.dump_id < %s AND pe.dump_id > %s """,
    (server_ip, dump_id, dump_id-MAX_PAST_DUMPS))
    server_ip_total_hashes = cursor.fetchone()[0]
    if server_ip_total_hashes != 0:
        server_ip_unknown_hash_ratio = float(server_ip_unknown_hashes) / server_ip_total_hashes
    else:
        server_ip_unknown_hash_ratio = None
    try:
        cursor.execute("""
                UPDATE weka_features set server_ip_benign_downloads = %s,
                 server_ip_malware_downloads = %s,
                 server_ip_suspicious_downloads = %s,
                 server_ip_total_downloads = %s,
                 server_ip_malware_ratio = %s,
                 server_ip_suspicious_ratio = %s,
                 server_ip_benign_ratio = %s,
                 server_ip_avg_av_labels = %s,
                 server_ip_avg_trusted_labels = %s,
                 server_ip_unknown_hashes = %s,
                 server_ip_total_hashes = %s,
                 server_ip_unknown_hash_ratio = %s
                 where dump_id = %s """,
                (server_ip_benign_downloads, server_ip_malware_downloads, 
                 server_ip_suspicious_downloads,
                 server_ip_total_downloads, server_ip_malware_ratio,
                 server_ip_suspicious_ratio,
                 server_ip_benign_ratio, 
                 server_ip_avg_av_labels, server_ip_avg_trusted_labels,
                 server_ip_unknown_hashes, server_ip_total_hashes,
                 server_ip_unknown_hash_ratio, dump_id))
    except Exception as e:
        print e
        print "Could not insert server_ip based features for the dump #", dump_id

def insert_bgp_based_features(cursor, dump_id):

    cursor.execute("""
            SELECT server from pe_dumps where dump_id = %s""", (dump_id, ))
    server = cursor.fetchone()[0]

    cursor.execute("""
                    select bgp_prefix from bgp2asn where bgp_prefix >> %s""", (server,))
    row = cursor.fetchone()
    if row is not None:
        bgp_prefix = row[0]
    else:
        return

    cursor.execute("""
        SELECT COUNT(DISTINCT dump_id)
        FROM pe_dumps AS pe
        WHERE pe.server << %s AND
            pe.corrupt = 'f' AND
            pe.dump_id < %s AND pe.dump_id > %s """,
        (bgp_prefix, dump_id, dump_id-MAX_PAST_DUMPS))
    bgp_total_downloads = cursor.fetchone()[0]

    # Disabled vt_month_shelf due to the 403 error from VT
    #cursor.execute("""
    #    SELECT count(distinct dump_id) from pe_dumps as pe JOIN 
    #    weka_features as f using (dump_id)
    #    where f.raw_dump_num_av_labels = 0 and f.vt_month_shelf = 't' and  
    #    pe.server << %s and pe.dump_id < %s """,
    #    (bgp_prefix, dump_id))
    cursor.execute("""
        SELECT COUNT(DISTINCT dump_id)
        FROM pe_dumps AS pe JOIN
            ped_vts_mapping AS pvm USING (dump_id),
            virus_total_scans AS vts
        WHERE vts.num_av_labels = 0 AND
            pe.corrupt = 'f' AND
            pe.server << %s AND
            pe.dump_id < %s AND pe.dump_id > %s AND 
            vts.vt_id = pvm.vt_id""",
        (bgp_prefix, dump_id, dump_id-MAX_PAST_DUMPS))
    bgp_benign_downloads = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(DISTINCT dump_id)
        FROM pe_dumps AS pe JOIN
            ped_vts_mapping AS pvm USING (dump_id),
            virus_total_scans AS vts
        WHERE vts.trusted_av_labels > 1 AND
            pe.corrupt = 'f' AND
            pe.server << %s AND
            pe.dump_id < %s AND pe.dump_id > %s AND
            vts.vt_id = pvm.vt_id""",
        (bgp_prefix, dump_id, dump_id-MAX_PAST_DUMPS))
    bgp_malware_downloads = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(DISTINCT dump_id)
        FROM pe_dumps AS pe JOIN
            ped_vts_mapping AS pvm USING (dump_id),
            virus_total_scans AS vts
        WHERE vts.num_av_labels > 1 AND
            pe.corrupt = 'f' AND
            pe.server << %s AND
            pe.dump_id < %s AND pe.dump_id > %s AND 
            vts.vt_id = pvm.vt_id""",
        (bgp_prefix, dump_id, dump_id-MAX_PAST_DUMPS))
    bgp_suspicious_downloads = cursor.fetchone()[0]

    if bgp_total_downloads == 0:
        bgp_benign_ratio = None
        bgp_malware_ratio = None
        bgp_suspicious_ratio = None
    else:
        bgp_benign_ratio = float(bgp_benign_downloads) / bgp_total_downloads
        bgp_malware_ratio = float(bgp_malware_downloads) / bgp_total_downloads
        bgp_suspicious_ratio = float(bgp_suspicious_downloads) / bgp_total_downloads

    # The averages are over distinct sha1s
    cursor.execute("""
        SELECT AVG(num_av_labels), AVG(trusted_av_labels)
        FROM
            (SELECT pe.sha1, MAX(dump_id) AS max_id
            FROM pe_dumps AS pe
            WHERE pe.server << %s AND
                pe.dump_id < %s AND pe.dump_id > %s AND
                pe.corrupt = 'f' GROUP BY pe.sha1) as a
            JOIN
            (SELECT p.sha1, num_av_labels, trusted_av_labels, dump_id
            FROM pe_dumps AS p JOIN
                ped_vts_mapping as pvm USING (dump_id),
                virus_total_scans as vts
            WHERE pvm.vt_id = vts.vt_id AND
                p.server << %s AND
                dump_id < %s AND dump_id > %s AND
                p.corrupt='f') as b
            ON a.max_id = b.dump_id
        WHERE num_av_labels IS NOT NULL""",
    (bgp_prefix, dump_id, dump_id-MAX_PAST_DUMPS, bgp_prefix, dump_id, dump_id-MAX_PAST_DUMPS))
    if cursor.rowcount == 0:
        bgp_avg_av_labels = None
        bgp_avg_trusted_labels = None
    else:
        bgp_avg_av_labels, bgp_avg_trusted_labels = cursor.fetchone()

    # the oldest scan report is used to get the # of unknown hashes
    # to remove any bias due to VT submissions
    cursor.execute("""
        SELECT COUNT(DISTINCT b.sha1)
        FROM
            (SELECT pe.sha1, MIN(dump_id) AS min_id
            FROM pe_dumps AS pe
            WHERE pe.server << %s AND
                pe.dump_id < %s AND pe.dump_id > %s AND
                pe.corrupt = 'f' GROUP BY pe.sha1) as a
            JOIN
            (SELECT p.sha1, num_av_labels, trusted_av_labels, dump_id
            FROM pe_dumps AS p JOIN
                ped_vts_mapping as pvm USING (dump_id),
                virus_total_scans as vts
            WHERE pvm.vt_id = vts.vt_id AND
                p.server << %s AND
                dump_id < %s AND dump_id > %s AND
                p.corrupt='f') as b
            ON a.min_id = b.dump_id
        WHERE num_av_labels IS NULL""",
    (bgp_prefix, dump_id, dump_id-MAX_PAST_DUMPS, bgp_prefix, dump_id, dump_id-MAX_PAST_DUMPS))
    bgp_unknown_hashes = cursor.fetchone()[0]
    
    cursor.execute("""
        SELECT COUNT(DISTINCT pe.sha1)
        FROM pe_dumps AS pe
        WHERE pe.server << %s AND
            pe.corrupt = 'f' AND
            pe.dump_id < %s AND pe.dump_id > %s """,
    (bgp_prefix, dump_id, dump_id-MAX_PAST_DUMPS))
    bgp_total_hashes = cursor.fetchone()[0]
    if bgp_total_hashes != 0:
        bgp_unknown_hash_ratio = float(bgp_unknown_hashes) / bgp_total_hashes
    else:
        bgp_unknown_hash_ratio = None
    try:
        cursor.execute("""
                UPDATE weka_features set bgp_benign_downloads = %s,
                 bgp_malware_downloads = %s,
                 bgp_suspicious_downloads = %s,
                 bgp_total_downloads = %s,
                 bgp_malware_ratio = %s,
                 bgp_suspicious_ratio = %s,
                 bgp_benign_ratio = %s,
                 bgp_avg_av_labels = %s,
                 bgp_avg_trusted_labels = %s,
                 bgp_unknown_hashes = %s,
                 bgp_total_hashes = %s,
                 bgp_unknown_hash_ratio = %s
                 where dump_id = %s """,
                (bgp_benign_downloads, bgp_malware_downloads, 
                 bgp_suspicious_downloads,
                 bgp_total_downloads, bgp_malware_ratio,
                 bgp_suspicious_ratio,
                 bgp_benign_ratio, 
                 bgp_avg_av_labels, bgp_avg_trusted_labels, 
                 bgp_unknown_hashes, bgp_total_hashes,
                 bgp_unknown_hash_ratio, dump_id))
    except:
        print "Could not insert bgp based features for the dump #", dump_id


def insert_hash_based_features(cursor, dump_id):
    cursor.execute("""select sha1 from pe_dumps where dump_id = %s""",
                   (dump_id, ))
    sha1 = cursor.fetchone()[0]
    if sha1 is None:
        return
    cursor.execute("""
        SELECT EXTRACT(EPOCH FROM (MAX(timestamp) - MIN(timestamp))),
            COUNT(DISTINCT pe.dump_id)
        FROM pe_dumps AS pe
        WHERE pe.dump_id < %s AND pe.dump_id > %s AND
            pe.corrupt = 'f' AND
            pe.sha1 = %s AND
            pe.corrupt = 'f' """,
        (dump_id, dump_id-MAX_PAST_DUMPS, sha1))
    hash_life_time, num_dumps_with_same_hash = cursor.fetchone()

    if hash_life_time is None:
        hash_life_time = 0
    if num_dumps_with_same_hash is None:
        num_dumps_with_same_hash = 0

    cursor.execute("""
        UPDATE weka_features
        SET hash_life_time = %s,
            num_dumps_with_same_hash = %s
        WHERE dump_id = %s""",
        (hash_life_time, num_dumps_with_same_hash, dump_id))

    cursor.execute("""
        SELECT count(*) FROM
            (SELECT DISTINCT client,
                DATE_TRUNC('DAY', timestamp)
            FROM pe_dumps AS pe
            WHERE pe.dump_id < %s AND pe.dump_id > %s AND
                pe.corrupt='f' AND
                pe.sha1 = %s) AS a""",
        (dump_id, dump_id-MAX_PAST_DUMPS, sha1))
    estimated_clients_with_same_hash = cursor.fetchone()[0]

    cursor.execute("""
        SELECT AVG(count)
        FROM
            (SELECT client,
                date_trunc('day', timestamp) AS ts,
                COUNT(*)
            FROM pe_dumps AS pe
            WHERE pe.dump_id < %s AND pe.dump_id > %s AND
                pe.corrupt='f' AND
                pe.sha1 = %s
            GROUP BY client, ts) AS b""",
        (dump_id, dump_id-MAX_PAST_DUMPS, sha1))
    hash_daily_dump_rate_per_client = cursor.fetchone()[0]

    cursor.execute("""
        UPDATE weka_features
        SET estimated_clients_with_same_hash = %s,
            hash_daily_dump_rate_per_client = %s
        WHERE dump_id = %s""",
        (estimated_clients_with_same_hash, hash_daily_dump_rate_per_client,
        dump_id))


def insert_download_request_features(cursor, dump_id):
    cursor.execute("""
        SELECT *
        FROM pe_dumps
        WHERE dump_id = %s AND
            referer IS null""",
        (dump_id,))
    if cursor.rowcount == 1:
        referer_exists = 0
    else:
        referer_exists = 1

    # update weka_features as wf set host_name_exists=0 from pe_dumps as pe
    # where pe.dump_id = wf.dump_id and host SIMILAR TO
    # '[0-9]+.[0-9]+.[0-9]+.[0-9]+'
    cursor.execute("""
        SELECT *
        FROM pe_dumps
        WHERE dump_id = %s AND
            host = SUBSTRING(CAST(server AS TEXT) FROM '(.*)/32')""",
        (dump_id,))
    if cursor.rowcount == 0:
        host_name_exists = 1
    else:
        host_name_exists = 0

    cursor.execute("""
        UPDATE weka_features
        SET referer_exists = %s,
            host_name_exists = %s
        WHERE dump_id = %s""",
        (referer_exists, host_name_exists, dump_id))

    # Once we generalize to file types beyond PE files, the extension_class feature should probably be removed
    common_ext = ['exe', 'dll', 'msi', 'jar', 'dmg', 'apk'] # executable files extensions...
    common_fake = ['html', 'gif', 'jpg', 'jpeg', 'txt', 'pdf', 'htm']
    other_ext = ['php', 'aspx', 'asp']

    cursor.execute("""
        SELECT url
        FROM pe_dumps
        WHERE dump_id = %s""",
        (dump_id,))
    url = cursor.fetchone()[0]
    if url is not None:
        ext = util.extract_extension(url)
        if ext is not None:
            ext = ext[:10]

        if ext is None:
            ext_class = "no_ext"
        elif ext in common_ext:
            ext_class = "common_ext"
        elif ext in common_fake:
            ext_class = "common_fake"
        elif ext in other_ext:
            ext_class = "other_ext"
        else:
            ext_class = "unknown_ext"
        #print "url:", url
        #print "extension:", ext
    else:
        ext_class = "no_url"
        ext = None
    cursor.execute("""
        UPDATE weka_features
        SET extension_class = %s
        WHERE dump_id = %s""",
        (ext_class, dump_id))

    cursor.execute("""
        SELECT CHAR_LENGTH(url), url
        FROM pe_dumps
        WHERE dump_id = %s""",
        (dump_id,))
    row = cursor.fetchone()
    url_length = None 
    if row is not None:
        url_length = row[0]
        url = row[1]
        if url is not None:
            url_path = url.split('?')[0]
            directory_depth = url_path.count('/')
        else:
            url_length = 0
            directory_depth = 0
    
    cursor.execute("""
            UPDATE weka_features SET
            url_length = %s,
            directory_depth = %s
            WHERE dump_id = %s""",
            (url_length, directory_depth, dump_id))


def insert_url_features(cursor, dump_id):
#    cursor.execute("SELECT ")
    cursor.execute("SELECT url from pe_dumps where dump_id = %s", (dump_id,))
    url = cursor.fetchone()[0]
    cursor.execute("""
        SELECT COUNT(DISTINCT dump_id)
        FROM pe_dumps AS pe JOIN
            ped_vts_mapping AS pvm USING (dump_id),
            virus_total_scans AS vts
        WHERE vts.trusted_av_labels > 1 AND
            pe.corrupt = 'f' AND
            pe.url = %s AND
            pe.dump_id < %s AND pe.dump_id > %s AND
            pvm.vt_id = vts.vt_id """,
        (url, dump_id, dump_id-MAX_PAST_DUMPS))
    url_malware_downloads = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(DISTINCT dump_id)
        FROM pe_dumps AS pe
        WHERE pe.url = %s AND
            pe.corrupt = 'f' AND
            pe.dump_id < %s AND pe.dump_id > %s """,
        (url, dump_id, dump_id-MAX_PAST_DUMPS))
    url_total_downloads = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(DISTINCT pe.sha1)
        FROM pe_dumps AS pe
        WHERE pe.url = %s AND
            pe.corrupt = 'f' AND
            pe.dump_id < %s AND pe.dump_id > %s AND
            pe.corrupt='f' """,
        (url, dump_id, dump_id-MAX_PAST_DUMPS))
    url_distinct_sha1s = cursor.fetchone()[0]

    cursor.execute("""
        UPDATE weka_features
        SET url_malware_downloads = %s,
            url_total_downloads = %s,
            url_distinct_sha1s = %s
        WHERE dump_id = %s """,
    (url_malware_downloads, url_total_downloads,
    url_distinct_sha1s, dump_id))


def get_url_struct_matches(cursor, url_struct, dump_id):
    # escaping special regex characters
    replace = [
               ('.', '\.'), ('+', '\+'), ('?', '\?'),
               ('{', '\{'), ('}', '\}'), ('[', '\]'),
               ('[', '\]'), ('^', '\^'), ('$', '\$')
              ]
    for pair in replace:
        url_struct = url_struct.replace(pair[0], pair[1])
    # the structure should be a matched to the whole query path
    url_struct = '^.*\?' + url_struct + '$'
    #print "The formatted url_struct: %s" % (url_struct,)
    cursor.execute("""
        SELECT COUNT(DISTINCT dump_id)
        FROM pe_dumps AS pe JOIN
            ped_vts_mapping AS pvm USING (dump_id),
            virus_total_scans AS vts
        WHERE vts.trusted_av_labels > 1 AND
            pvm.vt_id = vts.vt_id AND
            pe.corrupt = 'f' AND
            pe.url ~ %s AND
            pe.dump_id < %s AND pe.dump_id > %s """,
        (url_struct, dump_id, dump_id-MAX_PAST_DUMPS))
    url_struct_malware_downloads = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(DISTINCT dump_id)
        FROM pe_dumps AS pe
        WHERE pe.url ~ %s AND
            pe.corrupt = 'f' AND
            pe.dump_id < %s AND pe.dump_id > %s """,
        (url_struct, dump_id, dump_id-MAX_PAST_DUMPS))
    url_struct_total_downloads = cursor.fetchone()[0]

    cursor.execute("""
        SELECT COUNT(DISTINCT pe.sha1)
        FROM pe_dumps AS pe
        WHERE pe.url ~ %s AND
            pe.dump_id < %s AND pe.dump_id > %s AND
            pe .corrupt='f' """,
        (url_struct, dump_id, dump_id-MAX_PAST_DUMPS))
    url_struct_distinct_sha1s = cursor.fetchone()[0]
    return (url_struct_malware_downloads, url_struct_total_downloads,
            url_struct_distinct_sha1s)


def insert_url_struct_matches(cursor, pmd, ptd, pds, dump_id):
    sql_query = "UPDATE weka_features " \
                "SET url_struct_malware_downloads = %s, " \
                "url_struct_total_downloads = %s, " \
                "url_struct_distinct_sha1s = %s " \
                "WHERE dump_id = %s" % \
                (pmd, ptd, pds, dump_id)
    cursor.execute(sql_query)


def insert_url_struct_features(cursor, dump_id):
    cursor.execute("""
                SELECT url from pe_dumps where dump_id = %s""", (dump_id,))
    url = cursor.fetchone()
    if url is None:
        return

    url = url[0]
    if url is None:
        return
    #print "The url is: ", url
    #print "Dump_id is ", dump_id
    #print "The parsed result is:", urlparse.urlparse(url)
    parsed_url = urlparse.urlparse(url)
    path = parsed_url.path
    #print "Path: ", path
    query = parsed_url.query
    query_list = urlparse.parse_qsl(query, keep_blank_values=True)
    #print "The parsed query is:",query_list

    #print "Query is: %s" % query
    m = re.search('([^\w]*)([\w]+)([^\w]+)(.*)', query)
    if m is None:
        print "No url_struct found!"
        return
    first_exp = m.group(1)
    word = m.group(2)
    divide = m.group(3)
    rest = m.group(4)
    url_struct = None
    if first_exp is not None:
        url_struct = first_exp
    if rest is not None:
        url_struct += "\w*" + divide
    while True:
        m = re.search('([\w]+)([^\w]+)?(.*)', rest)
        if m is not None:
            word = m.group(1)
            divide = m.group(2)
            #if '.' in divide:
            #print "divide:", divide
            rest = m.group(3)
            if divide:
                url_struct += "\w*" + divide
            else:
                url_struct += "\w*"
        else: break

    #print "url_struct :", url_struct
    if len(url_struct) < 10:
        print "url_struct pattern length too short:%s, " % len(url_struct), url_struct
        return

    pmd, ptd, pds = get_url_struct_matches(cursor, url_struct, dump_id)
    print "Number of url_struct matching dumps: %s/%s" % (pmd,ptd)
    insert_url_struct_matches(cursor, pmd, ptd, pds, dump_id)


# TODO: currently file_type is not used. 
#       We will need to see if some of the features should be modified
#       to take the file_type into account
def insert_features(cursor, dump_id, file_type):
    print "the dump_id is:", dump_id
    cursor.execute("""
        DELETE FROM weka_features
        WHERE dump_id = %s
        """, (dump_id,))
    cursor.execute("""
    INSERT INTO weka_features (dump_id, corrupt, sha1, host)
        (SELECT pe.dump_id, pe.corrupt, pe.sha1, pe.host
            FROM pe_dumps AS pe
            WHERE pe.dump_id = %s )""",
        (dump_id,))
    #print "Inserted dump_id", cursor.fetchone()[0]

    insert_host_based_features(cursor, dump_id)
    insert_server_ip_based_features(cursor, dump_id)
    insert_bgp_based_features(cursor, dump_id)
    insert_twold_based_features(cursor, dump_id)
    insert_hash_based_features(cursor, dump_id)
    insert_download_request_features(cursor, dump_id)
    insert_url_features(cursor, dump_id)
    try:
        insert_url_struct_features(cursor, dump_id)
    except psycopg2.DataError as e:
        print "Exception in inserting url_struct features for %s dump_id" % (dump_id,)
        print e


def get_feature_vector(dump_id, file_type):
    #print "entered get_feature_vector"
    conn = util.connect_to_db()
    cursor = conn.cursor()
    insert_features(cursor, dump_id, file_type)
    print "Done inserting features for dump_id: ", dump_id

if __name__ == "__main__":
    if len(sys.argv) == 2:
        get_feature_vector(sys.argv[1])
    else:
        print "Incorrect number of arguments!!"
