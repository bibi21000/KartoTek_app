# -*- encoding: utf-8 -*-

def split_ids(ids):
    ret = []
    for idd in ids:
        if '-' in idd:
            id1,id2 = idd.split('-')
            ret.extend(list(range(int(id1), int(id2)+1)))
        else:
            ret.append(idd)
    return ret

