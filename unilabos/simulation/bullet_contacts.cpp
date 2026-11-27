// Prototype bridge to Blender's OWN Bullet symbols; no Bullet library linked.
// Layout and headers pinned to Blender d13f752e3b9c (v5.2.2), double precision.
#include <btBulletDynamicsCommon.h>
#include <cstdint>
struct rbDynamicsWorld {
  btDiscreteDynamicsWorld *dynamicsWorld;
  btDefaultCollisionConfiguration *collisionConfiguration;
  btDispatcher *dispatcher;
};
struct Contact {
  void *a, *b;
  double position[3], normal[3], separation, impulse;
};
extern "C" int lab_contacts(rbDynamicsWorld *w, Contact *out, int capacity) {
  int count=0;
  for(int i=0;i<w->dispatcher->getNumManifolds();i++) {
    auto *m=w->dispatcher->getManifoldByIndexInternal(i);
    for(int j=0;j<m->getNumContacts();j++) {
      auto &p=m->getContactPoint(j);
      if(count>=capacity) return -count-1;
      auto &c=out[count++];
      c.a=m->getBody0()->getUserPointer();c.b=m->getBody1()->getUserPointer();
      for(int k=0;k<3;k++){c.position[k]=p.getPositionWorldOnB()[k];c.normal[k]=p.m_normalWorldOnB[k];}
      c.separation=p.getDistance();c.impulse=p.getAppliedImpulse();
    }
  }
  return count;
}
extern "C" int lab_scalar_bytes(){return sizeof(btScalar);}
extern "C" void lab_friction_2d(rbDynamicsWorld *w){w->dynamicsWorld->getSolverInfo().m_solverMode |= SOLVER_USE_2_FRICTION_DIRECTIONS;}
struct rbRigidBody {btRigidBody *body;int groups;};
extern "C" void lab_inertia(rbRigidBody *b,double mass,double x,double y,double z){b->body->setMassProps(mass,btVector3(x,y,z));b->body->updateInertiaTensor();}
extern "C" void lab_ignore(rbRigidBody *a,rbRigidBody *b,int ignore){a->body->setIgnoreCollisionCheck(b->body,ignore!=0);b->body->setIgnoreCollisionCheck(a->body,ignore!=0);}
extern "C" void lab_force_at(rbRigidBody *b,const double *force,const double *point){
  b->body->applyForce(btVector3(force[0],force[1],force[2]),btVector3(point[0],point[1],point[2])-b->body->getCenterOfMassPosition());
}
extern "C" void *lab_ray(rbDynamicsWorld *w,const double *from,const double *to,void *exclude,double *distance){
  btVector3 a(from[0],from[1],from[2]),b(to[0],to[1],to[2]);
  btCollisionWorld::AllHitsRayResultCallback cb(a,b);w->dynamicsWorld->rayTest(a,b,cb);
  void *found=nullptr;*distance=1e9;
  for(int i=0;i<cb.m_collisionObjects.size();i++){
    void *id=cb.m_collisionObjects[i]->getUserPointer();
    double d=cb.m_hitFractions[i]*(b-a).length();
    if(id!=exclude && d>=.005 && d<=.1 && d<*distance){found=id;*distance=d;}
  }
  return found;
}
